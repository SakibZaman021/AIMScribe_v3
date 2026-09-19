"""
AIMScribe Integration Test Script
Simulates the AIMScribe Desktop App flow:
1. Create Session
2. Upload Audio (via MinIO Presigned URL)
3. Notify Backend
4. Poll for Processing Status
"""

import os
import sys
import time
import requests
import json
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Config
API_URL = "http://localhost:6000"
TEST_AUDIO_FILE = "test_audio.wav"

def create_dummy_audio():
    """Create a dummy WAV file for testing if not exists."""
    if not os.path.exists(TEST_AUDIO_FILE):
        logger.info(f"Creating dummy audio file: {TEST_AUDIO_FILE}")
        # Create a tiny valid WAV file (byte header)
        with open(TEST_AUDIO_FILE, "wb") as f:
            # Minimal WAV header (44 bytes) for a silent PCM file
            f.write(b'\x52\x49\x46\x46\x24\x00\x00\x00\x57\x41\x56\x45\x66\x6d\x74\x20'
                    b'\x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00'
                    b'\x02\x00\x10\x00\x64\x61\x74\x61\x00\x00\x00\x00')

def run_test():
    """Run full integration test."""
    logger.info("Starting AIMScribe Integration Test...")
    
    # Check health
    try:
        resp = requests.get(f"{API_URL}/health")
        if resp.status_code != 200:
            logger.error("Backend unhealthy. Is Docker running?")
            return
        logger.info(f"Health Check: {resp.json()}")
    except requests.exceptions.ConnectionError:
        logger.error("Could not connect to backend. Start Docker first!")
        return

    # 1. Create Session
    logger.info("\n--- Step 1: Create Session ---")
    resp = requests.post(f"{API_URL}/api/v1/session/create", json={
        "patient_id": "P_TEST_001",
        "doctor_id": "DR_TEST",
        "hospital_id": "HOSP_TEST"
    })
    if resp.status_code != 200:
        logger.error(f"Failed to create session: {resp.text}")
        return
    
    session_data = resp.json()
    session_id = session_data['session_id']
    logger.info(f"Session Created: {session_id}")
    
    # 2. Upload Audio Flow (Simulate 2 Clips)
    create_dummy_audio()
    
    for clip_num in [1, 2]:
        logger.info(f"\n--- Step 2: Processing Clip {clip_num} ---")
        
        # A. Request Upload URL
        logger.info(f"Requesting upload URL for clip {clip_num}...")
        resp = requests.post(f"{API_URL}/api/v1/upload/request", json={
            "session_id": session_id,
            "clip_number": clip_num
        })
        upload_data = resp.json()
        upload_url = upload_data['upload_url']
        object_key = upload_data['object_key']
        logger.info(f"Got Presigned URL: {upload_url[:50]}...")
        
        # B. Upload File to MinIO
        logger.info(f"Uploading file directly to MinIO...")
        with open(TEST_AUDIO_FILE, "rb") as f:
            headers = {"Content-Type": "audio/wav"}
            put_resp = requests.put(upload_url, data=f, headers=headers)
            if put_resp.status_code != 200:
                logger.error(f"MinIO Upload Failed: {put_resp.status_code}")
                return
        logger.info("Upload Successful")
        
        # C. Notify Backend to Process
        logger.info("Notifying backend to start processing...")
        resp = requests.post(f"{API_URL}/api/v1/upload/complete", json={
            "session_id": session_id,
            "clip_number": clip_num,
            "object_key": object_key,
            "is_final": (clip_num == 2)
        })
        job_data = resp.json()
        logger.info(f"Job Queued: {job_data}")
        
        # D. Poll for Status
        logger.info("Polling for completion...")
        max_retries = 30
        for _ in range(max_retries):
            status_resp = requests.get(f"{API_URL}/api/v1/session/{session_id}/status")
            status = status_resp.json()
            
            logger.info(f"Status: Clips={status['total_clips_transcribed']}, NER={status['has_ner']}")
            
            if status['total_clips_transcribed'] >= clip_num:
                logger.info(f"Clip {clip_num} transcribed!")
                if status['has_ner']:
                    logger.info("NER Extraction Complete!")
                break
            
            time.sleep(2)

    # 3. Get Final Results
    logger.info("\n--- Step 3: Final Results ---")
    
    # Get Transcript
    trans_resp = requests.get(f"{API_URL}/api/v1/transcript/{session_id}")
    logger.info(f"Full Transcript: {trans_resp.json()['transcript'][:100]}...")
    
    # Get NER
    ner_resp = requests.get(f"{API_URL}/api/v1/ner/{session_id}")
    logger.info("NER JSON Result:")
    print(json.dumps(ner_resp.json(), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    run_test()
