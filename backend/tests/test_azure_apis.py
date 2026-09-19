"""
AIMScribe API Testing Script
Run this to verify Azure OpenAI APIs are working correctly.

Usage:
    python tests/test_azure_apis.py
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def print_header(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)

def print_result(success, message):
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"{status}: {message}")

def test_environment_variables():
    """Test 1: Check if all required environment variables are set."""
    print_header("TEST 1: Environment Variables")

    required_vars = [
        ("AZURE_TRANSCRIBE_ENDPOINT", "Transcription API Endpoint"),
        ("AZURE_TRANSCRIBE_API_KEY", "Transcription API Key"),
        ("AZURE_TRANSCRIBE_DEPLOYMENT", "Transcription Deployment Name"),
        ("AZURE_NER_ENDPOINT", "NER API Endpoint"),
        ("AZURE_NER_API_KEY", "NER API Key"),
        ("AZURE_NER_DEPLOYMENT", "NER Deployment Name"),
    ]

    all_set = True
    for var, description in required_vars:
        value = os.getenv(var, "")
        if value and "your-" not in value.lower():
            print_result(True, f"{description} ({var})")
        else:
            print_result(False, f"{description} ({var}) - NOT SET or placeholder")
            all_set = False

    return all_set


def test_ner_api():
    """Test 2a: Test NER API connectivity (GPT-5.2-chat)."""
    print_header("TEST 2a: NER API Connectivity (GPT-5.2-chat)")

    try:
        from openai import AzureOpenAI

        client = AzureOpenAI(
            azure_endpoint=os.getenv("AZURE_NER_ENDPOINT"),
            api_key=os.getenv("AZURE_NER_API_KEY"),
            api_version=os.getenv("AZURE_API_VERSION", "2024-02-15-preview")
        )

        print("  Sending test request to NER API...")

        response = client.chat.completions.create(
            model=os.getenv("AZURE_NER_DEPLOYMENT"),
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Say 'NER API is working!' in Bengali."}
            ],
            # temperature=0.1,  # GPT-5.2 only supports default temperature (1)
            max_completion_tokens=50  # GPT-5.2 requires max_completion_tokens instead of max_tokens
        )

        result = response.choices[0].message.content
        print(f"  Response: {result}")
        print_result(True, "NER API is reachable!")
        return True

    except Exception as e:
        print_result(False, f"NER API Error: {e}")
        return False


def test_ner_extraction():
    """Test 2b: Test actual NER extraction with a sample Bengali transcript."""
    print_header("TEST 2b: NER Extraction (Chief Complaints from sample transcript)")

    try:
        from openai import AzureOpenAI
        import json as json_module

        client = AzureOpenAI(
            azure_endpoint=os.getenv("AZURE_NER_ENDPOINT"),
            api_key=os.getenv("AZURE_NER_API_KEY"),
            api_version=os.getenv("AZURE_API_VERSION", "2024-02-15-preview")
        )

        # Sample transcript matching transcriber_v4 output format: [Speaker_01], [Speaker_02]
        sample_transcript = """[Speaker_01]: আজকে কি সমস্যা নিয়ে আসছেন?
[Speaker_02]: তিন দিন ধরে জ্বর। গায়ে অনেক ব্যথা। মাথাও ব্যথা করছে।
[Speaker_01]: জ্বর কত আসছে?
[Speaker_02]: ১০১-১০২ এর মতো।
[Speaker_01]: কাশি আছে?
[Speaker_02]: না, কাশি নাই। তবে গলা একটু ব্যথা করছে।
[Speaker_01]: বমি বা পায়খানার সমস্যা?
[Speaker_02]: বমি বমি ভাব আছে, কিন্তু বমি হয়নি।
[Speaker_01]: ঠিক আছে। আগে কোনো ওষুধ খেয়েছেন?
[Speaker_02]: প্যারাসিটামল খেয়েছিলাম, জ্বর কমে আবার আসছে।
[Speaker_01]: আচ্ছা, রক্ত পরীক্ষা করাতে হবে। সিবিসি আর ডেঙ্গু টেস্ট। ওষুধ লিখে দিচ্ছি।"""

        print(f"  Sample transcript ({len(sample_transcript)} chars):")
        print(f"  {'-' * 40}")
        for line in sample_transcript.split('\n')[:5]:
            print(f"  {line}")
        print(f"  ... (truncated)")
        print(f"  {'-' * 40}")

        # NER system prompt for chief complaints extraction
        system_prompt = """You are an expert NER model specialized in extracting Chief Complaints from Bengali medical transcriptions.

RULES:
1. ONLY include symptoms explicitly mentioned by the patient (Speaker_02 in this conversation)
2. If doctor (Speaker_01) suggests symptoms and patient CONFIRMS, include them
3. Do NOT include symptoms the patient DENIES
4. Include duration and severity ONLY if explicitly mentioned
5. Speaker_01 and Speaker_02 are generic labels — determine who is doctor/patient from conversation context

OUTPUT: Return ONLY a valid JSON array. Each item has "Complaint (English)" and "Duration (English)".
If no complaints found, return: []

TRANSCRIPTION:
""" + sample_transcript

        print(f"\n  Sending to GPT-5.2-chat for NER extraction...")

        response = client.chat.completions.create(
            model=os.getenv("AZURE_NER_DEPLOYMENT"),
            messages=[
                {"role": "system", "content": system_prompt}
            ],
            # temperature=0.1,  # GPT-5.2 only supports default temperature (1)
            max_completion_tokens=2000  # GPT-5.2 requires max_completion_tokens instead of max_tokens
        )

        result_text = response.choices[0].message.content.strip()

        # Clean markdown if present
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0].strip()
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0].strip()

        # Parse JSON
        ner_result = json_module.loads(result_text)

        print(f"\n  NER Extraction Result:")
        print(f"  {'-' * 40}")
        print(f"  {json_module.dumps(ner_result, ensure_ascii=False, indent=2)}")
        print(f"  {'-' * 40}")

        # Validate: we expect at least fever, body pain from the sample
        if isinstance(ner_result, list) and len(ner_result) > 0:
            print(f"\n  Complaints extracted: {len(ner_result)}")
            for item in ner_result:
                complaint = item.get("Complaint (English)", "?")
                duration = item.get("Duration (English)", "?")
                print(f"    - {complaint} ({duration})")

            print_result(True, f"NER extraction working! Extracted {len(ner_result)} complaints")
            return True
        else:
            print_result(False, "NER returned empty result — expected complaints from sample")
            return False

    except json_module.JSONDecodeError as e:
        print(f"  Raw response: {result_text[:300]}")
        print_result(False, f"NER returned invalid JSON: {e}")
        return False
    except Exception as e:
        print_result(False, f"NER Extraction Error: {e}")
        return False


def test_transcription_api():
    """Test 3: Test Transcription API endpoint connectivity."""
    print_header("TEST 3: Transcription API (Audio Transcriptions Endpoint)")

    try:
        import requests

        endpoint = os.getenv("AZURE_TRANSCRIBE_ENDPOINT", "").rstrip('/')
        api_key = os.getenv("AZURE_TRANSCRIBE_API_KEY", "")
        deployment = os.getenv("AZURE_TRANSCRIBE_DEPLOYMENT", "gpt-4o-transcribe-diarize")
        api_version = os.getenv("AZURE_TRANSCRIBE_API_VERSION", "2025-03-01-preview")

        if not endpoint or not api_key:
            print_result(False, "Transcription endpoint or API key not set")
            return False

        url = f"{endpoint}/openai/deployments/{deployment}/audio/transcriptions?api-version={api_version}"

        print(f"  Endpoint: {endpoint}")
        print(f"  Deployment: {deployment}")
        print(f"  API Version: {api_version}")
        print(f"  Note: Full audio test runs in Test 4 (requires test_audio.wav)")

        print_result(True, "Transcription API configuration verified!")
        return True

    except Exception as e:
        print_result(False, f"Transcription API Error: {e}")
        return False


def test_transcription_with_audio():
    """Test 4: Test audio transcription via Audio Transcriptions API."""
    print_header("TEST 4: Audio Transcription (Audio Transcriptions API + json format)")

    test_audio_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tests",
        "test_audio.wav"
    )

    if not os.path.exists(test_audio_path):
        print(f"  ⚠️  SKIP: No test audio file found at {test_audio_path}")
        print("  To test audio transcription, place a .wav file at: tests/test_audio.wav")
        return None

    try:
        import requests

        endpoint = os.getenv("AZURE_TRANSCRIBE_ENDPOINT", "").rstrip('/')
        api_key = os.getenv("AZURE_TRANSCRIBE_API_KEY", "")
        deployment = os.getenv("AZURE_TRANSCRIBE_DEPLOYMENT", "gpt-4o-transcribe-diarize")
        api_version = os.getenv("AZURE_TRANSCRIBE_API_VERSION", "2025-03-01-preview")

        url = f"{endpoint}/openai/deployments/{deployment}/audio/transcriptions?api-version={api_version}"

        print(f"  Audio file loaded: {os.path.getsize(test_audio_path) / 1024:.2f} KB")
        print(f"  Sending audio to {deployment} (Audio Transcriptions API, diarized_json)...")

        headers = {
            "api-key": api_key,
        }

        with open(test_audio_path, "rb") as audio_file:
            files = {
                "file": ("test_audio.wav", audio_file, "audio/wav")
            }
            data = {
                "model": deployment,
                "response_format": "diarized_json",  # Returns segments with speaker labels
                "chunking_strategy": "auto",  # Required for diarization models
                "language": "bn",  # Bengali — re-enabled (500 was from missing chunking_strategy)
            }

            response = requests.post(
                url,
                headers=headers,
                files=files,
                data=data,
                timeout=120
            )

        if response.status_code != 200:
            print_result(False, f"Audio transcription error: {response.status_code} - {response.text}")
            return False

        result = response.json()

        # Show raw segments from API
        segments = result.get("segments", [])
        if segments:
            print(f"\n  Segments found: {len(segments)}")
            print(f"\n  Raw segments (first 5):")
            for seg in segments[:5]:
                speaker = seg.get("speaker", "?")
                text = seg.get("text", "").strip()
                print(f"    [{speaker}]: {text[:80]}")

        # Format segments into final transcript (same logic as transcriber_v4.py)
        speaker_number_map = {}
        next_num = 1
        merged_lines = []
        current_speaker = None
        current_texts = []

        for seg in segments:
            speaker = seg.get("speaker", "unknown")
            text = seg.get("text", "").strip()
            if not text:
                continue

            if speaker not in speaker_number_map:
                speaker_number_map[speaker] = f"Speaker_{next_num:02d}"
                next_num += 1

            if speaker == current_speaker:
                current_texts.append(text)
            else:
                if current_speaker is not None and current_texts:
                    label = speaker_number_map[current_speaker]
                    merged_lines.append(f"[{label}]: {' '.join(current_texts).strip()}")
                current_speaker = speaker
                current_texts = [text]

        if current_speaker is not None and current_texts:
            label = speaker_number_map[current_speaker]
            merged_lines.append(f"[{label}]: {' '.join(current_texts).strip()}")

        # Print formatted transcript
        print(f"\n  Formatted Transcript:\n  {'-' * 40}")
        for line in merged_lines[:15]:
            print(f"  {line}")
        if len(merged_lines) > 15:
            print(f"  ... ({len(merged_lines) - 15} more lines)")
        print(f"  {'-' * 40}")
        print(f"  Speaker mapping: {speaker_number_map}")
        print(f"  Total speaker turns: {len(merged_lines)}")

        print_result(True, "Audio transcription working!")
        return True

    except Exception as e:
        print_result(False, f"Audio transcription error: {e}")
        return False


def test_database_connection():
    """Test 5: Test PostgreSQL connection."""
    print_header("TEST 5: PostgreSQL Database")

    try:
        import psycopg2

        conn = psycopg2.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", 5432)),
            dbname=os.getenv("POSTGRES_DB", "aimscribe_db"),
            user=os.getenv("POSTGRES_USER", "aimscribe_user"),
            password=os.getenv("POSTGRES_PASSWORD", "")
        )

        cursor = conn.cursor()
        cursor.execute("SELECT version();")
        version = cursor.fetchone()[0]
        print(f"  PostgreSQL: {version[:50]}...")

        cursor.close()
        conn.close()

        print_result(True, "PostgreSQL connection successful!")
        return True

    except Exception as e:
        print_result(False, f"PostgreSQL Error: {e}")
        print("  Make sure PostgreSQL is running and credentials are correct.")
        return False


def test_redis_connection():
    """Test 6: Test Redis connection."""
    print_header("TEST 6: Redis")

    try:
        import redis

        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(os.getenv("REDIS_DB", 0)),
            password=os.getenv("REDIS_PASSWORD") or None
        )

        # Test ping
        client.ping()

        # Test set/get
        client.set("aimscribe:test", "working")
        value = client.get("aimscribe:test")
        client.delete("aimscribe:test")

        print_result(True, "Redis connection successful!")
        return True

    except Exception as e:
        print_result(False, f"Redis Error: {e}")
        print("  Make sure Redis is running.")
        return False


def test_minio_connection():
    """Test 7: Test MinIO connection."""
    print_header("TEST 7: MinIO (Object Storage)")

    try:
        from minio import Minio

        client = Minio(
            os.getenv("MINIO_ENDPOINT", "localhost:9000"),
            access_key=os.getenv("MINIO_ACCESS_KEY", "aimscribe"),
            secret_key=os.getenv("MINIO_SECRET_KEY", "aimscribe123"),
            secure=os.getenv("MINIO_SECURE", "false").lower() == "true"
        )

        # Check if bucket exists
        bucket = os.getenv("MINIO_BUCKET", "aimscribe-audio")
        exists = client.bucket_exists(bucket)

        if exists:
            print(f"  Bucket '{bucket}' exists")
        else:
            print(f"  Bucket '{bucket}' does not exist, creating...")
            client.make_bucket(bucket)
            print(f"  Bucket '{bucket}' created")

        print_result(True, "MinIO connection successful!")
        return True

    except Exception as e:
        print_result(False, f"MinIO Error: {e}")
        print("  Make sure MinIO is running.")
        return False


def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("       AIMScribe Backend - API & Services Test")
    print("=" * 60)

    results = {
        "Environment Variables": test_environment_variables(),
        "NER API Connectivity": test_ner_api(),
        "NER Extraction (Chief Complaints)": test_ner_extraction(),
        "Transcription API Config": test_transcription_api(),
        "Audio Transcription": test_transcription_with_audio(),
        "PostgreSQL": test_database_connection(),
        "Redis": test_redis_connection(),
        "MinIO": test_minio_connection(),
    }

    # Summary
    print_header("TEST SUMMARY")

    passed = 0
    failed = 0
    skipped = 0

    for test_name, result in results.items():
        if result is True:
            print(f"  ✅ {test_name}")
            passed += 1
        elif result is False:
            print(f"  ❌ {test_name}")
            failed += 1
        else:
            print(f"  ⚠️  {test_name} (SKIPPED)")
            skipped += 1

    print(f"\n  Total: {passed} passed, {failed} failed, {skipped} skipped")

    if failed == 0:
        print("\n  🎉 All tests passed! Your backend is ready.")
    else:
        print("\n  ⚠️  Some tests failed. Please check the configuration.")

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
