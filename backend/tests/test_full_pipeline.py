import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

"""
AIMScribe Full Pipeline Test
=============================
Tests the complete flow:
  1. Health check
  2. Create session → see it in PostgreSQL
  3. Upload audio → see it in MinIO
  4. Notify upload complete → worker processes it
  5. Poll for results → see transcript + NER in PostgreSQL

Prerequisites:
  - Docker services running (docker-compose up -d)
  - API server running (docker compose up -d api)
  - Worker running (docker compose up -d worker)
"""

import os
import sys
import time
import json
import requests

# ================================================================
# Configuration
# ================================================================
API_BASE = "http://localhost:6000"
AUDIO_FILE = os.path.join(os.path.dirname(__file__), "test_audio.wav")

# Colors for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def header(text):
    print(f"\n{BOLD}{CYAN}{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}{RESET}\n")


def success(text):
    print(f"  {GREEN}OK{RESET} {text}")


def fail(text):
    print(f"  {RED}FAIL{RESET} {text}")


def info(text):
    print(f"  {YELLOW}--{RESET} {text}")


def print_json(data, indent=6):
    """Pretty print JSON with indentation."""
    formatted = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    for line in formatted.split("\n"):
        print(f"{' ' * indent}{line}")


# ================================================================
# Step 1: Health Check
# ================================================================
def test_health():
    header("STEP 1: Health Check")
    try:
        r = requests.get(f"{API_BASE}/health", timeout=5)
        data = r.json()
        print_json(data)

        if data.get("database") == "connected" and data.get("redis") == "connected":
            success("All services connected")
            return True
        else:
            fail("Some services disconnected")
            return False
    except requests.ConnectionError:
        fail("Cannot connect to API server. Is the api container running?")
        return False


# ================================================================
# Step 2: Create Session
# ================================================================
def test_create_session():
    header("STEP 2: Create Session")

    payload = {
        "patient_id": "TEST_P001",
        "doctor_id": "DR_KARIM",
        "hospital_id": "AIMS_LAB"
    }
    info(f"Creating session for patient: {payload['patient_id']}")

    r = requests.post(f"{API_BASE}/api/v1/session/create", json=payload)
    data = r.json()
    print_json(data)

    if r.status_code == 200 and data.get("session_id"):
        session_id = data["session_id"]
        success(f"Session created: {session_id}")

        info("What happened in PostgreSQL:")
        info("  -> 'sessions' table: new row with UUID, patient_id=TEST_P001, status=active")
        info("  -> 'patients' table: new row for TEST_P001 (auto-created if not exists)")
        print(f"\n  {BOLD}Go check pgAdmin:{RESET}")
        print(f"    SELECT * FROM sessions;")
        print(f"    SELECT * FROM patients;")

        return session_id
    else:
        fail(f"Session creation failed: {data}")
        return None


# ================================================================
# Step 3: Request Upload URL + Upload Audio to MinIO
# ================================================================
def test_upload_audio(session_id):
    header("STEP 3: Upload Audio to MinIO")

    # 3a: Get presigned upload URL
    info("Requesting presigned upload URL...")
    r = requests.post(f"{API_BASE}/api/v1/upload/request", json={
        "session_id": session_id,
        "clip_number": 1
    })
    data = r.json()

    upload_url = data.get("upload_url")
    object_key = data.get("object_key")

    if not upload_url:
        fail(f"Failed to get upload URL: {data}")
        return None

    success(f"Got upload URL (expires in {data.get('expires_in')}s)")
    info(f"Object key: {object_key}")

    # 3b: Upload the audio file
    info(f"Uploading audio file: {AUDIO_FILE}")
    file_size = os.path.getsize(AUDIO_FILE) / 1024
    info(f"File size: {file_size:.1f} KB")

    with open(AUDIO_FILE, "rb") as f:
        r = requests.put(upload_url, data=f, headers={"Content-Type": "audio/wav"})

    if r.status_code == 200:
        success("Audio uploaded to MinIO!")
        info("What happened in MinIO:")
        info(f"  -> Bucket 'aimscribe-audio': file at '{object_key}'")
        print(f"\n  {BOLD}Go check MinIO Console:{RESET}")
        print(f"    http://localhost:9001  (login: aimscribe / aimscribe123)")
        print(f"    Object Browser -> aimscribe-audio -> audio/{session_id}/")

        info("What happened in PostgreSQL:")
        info("  -> 'clips' table: new row with status='pending'")
        print(f"\n  {BOLD}Go check pgAdmin:{RESET}")
        print(f"    SELECT * FROM clips;")

        return object_key
    else:
        fail(f"Upload failed: {r.status_code} {r.text}")
        return None


# ================================================================
# Step 4: Notify Upload Complete (Triggers Worker)
# ================================================================
def test_trigger_processing(session_id, object_key):
    header("STEP 4: Trigger Processing (notify upload complete)")

    info("Sending completion notification with is_final=true...")
    info("This pushes a job to Redis queue -> worker picks it up")

    r = requests.post(f"{API_BASE}/api/v1/upload/complete", json={
        "session_id": session_id,
        "clip_number": 1,
        "object_key": object_key,
        "is_final": True
    })
    data = r.json()
    print_json(data)

    if r.status_code == 200 and data.get("status") == "queued":
        success(f"Job queued! Job ID: {data.get('job_id')}")
        info("What happens now:")
        info("  1. Redis queue receives the job")
        info("  2. Worker pops the job from Redis")
        info("  3. Worker downloads audio from MinIO")
        info("  4. Worker sends audio to Azure gpt-4o-transcribe-diarize")
        info("  5. Worker saves Bengali transcript to PostgreSQL (clips + transcripts)")
        info("  6. Since is_final=true, worker runs NER extraction (9 modules in parallel)")
        info("  7. Worker saves NER JSON to PostgreSQL (ner_results)")
        info("  8. Worker archives to previous_visits")
        print(f"\n  {YELLOW}Watch the worker terminal for real-time logs!{RESET}")
        return True
    else:
        fail(f"Trigger failed: {data}")
        return False


# ================================================================
# Step 5: Poll for Results
# ================================================================
def test_poll_results(session_id):
    header("STEP 5: Waiting for Worker to Process...")

    max_wait = 120  # seconds
    poll_interval = 5
    elapsed = 0

    while elapsed < max_wait:
        r = requests.get(f"{API_BASE}/api/v1/session/{session_id}/status")
        data = r.json()

        status = data.get("status", "unknown")
        has_transcript = data.get("has_transcript", False)
        has_ner = data.get("has_ner", False)

        info(f"[{elapsed}s] status={status}, transcript={has_transcript}, ner={has_ner}")

        if has_ner:
            success("Processing complete!")
            break

        time.sleep(poll_interval)
        elapsed += poll_interval
    else:
        fail(f"Timed out after {max_wait}s. Check worker terminal for errors.")
        return False

    # ---- Show Transcript ----
    header("RESULT: Transcript")
    r = requests.get(f"{API_BASE}/api/v1/transcript/{session_id}")
    transcript_data = r.json()
    transcript = transcript_data.get("transcript", "")

    if transcript:
        success(f"Transcript received ({len(transcript)} characters)")
        print(f"\n{CYAN}--- Transcript ---{RESET}")
        print(transcript)
        print(f"{CYAN}--- End ---{RESET}")
    else:
        fail("No transcript found")

    info("Where this is stored in PostgreSQL:")
    info("  -> 'clips' table: clip_transcript column")
    info("  -> 'transcripts' table: full_transcript column")
    print(f"\n  {BOLD}Go check pgAdmin:{RESET}")
    print(f"    SELECT clip_transcript FROM clips WHERE session_id = '{session_id}';")
    print(f"    SELECT full_transcript FROM transcripts WHERE session_id = '{session_id}';")

    # ---- Show NER ----
    header("RESULT: NER Extraction (Individual Fields)")
    r = requests.get(f"{API_BASE}/api/v1/ner/{session_id}")
    ner_data = r.json()

    fields = ner_data.get("fields")
    if fields:
        success(f"NER result received (version {ner_data.get('version')}, final={ner_data.get('is_final')})")
        print(f"\n{CYAN}--- NER Fields (Individual Columns) ---{RESET}")
        for field_name, field_value in fields.items():
            print(f"  {BOLD}{field_name}{RESET}: ", end="")
            if isinstance(field_value, (dict, list)):
                print()
                print_json(field_value, indent=4)
            else:
                print(field_value)
        print(f"\n{CYAN}--- End ---{RESET}")

        # Also show full backup JSON
        full_json = ner_data.get("full_ner_json")
        if full_json:
            info("Full NER JSON backup also available in full_ner_json field")
    else:
        fail("No NER result found")

    info("Where this is stored in PostgreSQL:")
    info("  -> 'ner_results' table: individual columns (patient_name, diagnosis, etc.)")
    info("  -> 'ner_results' table: full_ner_json column (JSONB backup)")
    info("  -> 'previous_visits' table: archived for future reference")
    print(f"\n  {BOLD}Go check pgAdmin:{RESET}")
    print(f"    SELECT version, is_final, patient_name, diagnosis, medications FROM ner_results WHERE session_id = '{session_id}';")
    print(f"    SELECT * FROM previous_visits WHERE patient_id = 'TEST_P001';")

    # ---- Final Summary ----
    header("SUMMARY: Where to See Everything")
    print(f"  {BOLD}MinIO Console{RESET} (uploaded audio):")
    print(f"    URL:   http://localhost:9001")
    print(f"    Login: aimscribe / aimscribe123")
    print(f"    Path:  aimscribe-audio -> audio/{session_id}/clip_1.wav")
    print()
    print(f"  {BOLD}pgAdmin{RESET} (all database tables):")
    print(f"    sessions:        SELECT * FROM sessions WHERE session_id = '{session_id}';")
    print(f"    clips:           SELECT * FROM clips WHERE session_id = '{session_id}';")
    print(f"    transcripts:     SELECT * FROM transcripts WHERE session_id = '{session_id}';")
    print(f"    ner_results:      SELECT * FROM ner_results WHERE session_id = '{session_id}';")
    print(f"    doctor_reviews:   SELECT * FROM doctor_reviews WHERE session_id = '{session_id}';")
    print(f"    prescription_data:SELECT * FROM prescription_data WHERE session_id = '{session_id}';")
    print(f"    patients:         SELECT * FROM patients WHERE patient_id = 'TEST_P001';")
    print(f"    previous_visits:  SELECT * FROM previous_visits WHERE patient_id = 'TEST_P001';")

    return True


# ================================================================
# Main
# ================================================================
if __name__ == "__main__":
    print(f"\n{BOLD}{CYAN}")
    print("  ============================================")
    print("    AIMScribe Full Pipeline Test")
    print("    Audio -> Transcribe -> NER -> Database")
    print("  ============================================")
    print(f"{RESET}")

    # Check audio file exists
    if not os.path.exists(AUDIO_FILE):
        fail(f"Audio file not found: {AUDIO_FILE}")
        sys.exit(1)

    # Step 1
    if not test_health():
        print(f"\n{RED}Fix the health check issues before proceeding.{RESET}")
        sys.exit(1)

    # Step 2
    session_id = test_create_session()
    if not session_id:
        sys.exit(1)

    # Step 3
    object_key = test_upload_audio(session_id)
    if not object_key:
        sys.exit(1)

    # Step 4
    if not test_trigger_processing(session_id, object_key):
        sys.exit(1)

    # Step 5
    test_poll_results(session_id)

    print(f"\n{BOLD}{GREEN}Pipeline test complete!{RESET}\n")
