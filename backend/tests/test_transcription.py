"""
AIMScribe - Transcription API Test Script
Tests the Azure GPT-4o-Transcribe with Diarization API.

Usage:
    python tests/test_transcription.py
    python tests/test_transcription.py path/to/audio.wav
"""

import os
import sys
import json

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from dotenv import load_dotenv
load_dotenv()


def print_header(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_result(success, message):
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"{status}: {message}")


def test_environment():
    """Test 1: Check transcription environment variables."""
    print_header("TEST 1: Environment Variables")

    required_vars = [
        ("AZURE_TRANSCRIBE_ENDPOINT", "Transcription Endpoint"),
        ("AZURE_TRANSCRIBE_API_KEY", "Transcription API Key"),
        ("AZURE_TRANSCRIBE_DEPLOYMENT", "Transcription Deployment"),
        ("AZURE_TRANSCRIBE_API_VERSION", "Transcription API Version"),
    ]

    all_set = True
    for var, description in required_vars:
        value = os.getenv(var, "")
        if value and "your-" not in value.lower():
            # Mask sensitive values
            display_value = value[:20] + "..." if len(value) > 20 else value
            if "key" in var.lower():
                display_value = value[:8] + "****" + value[-4:] if len(value) > 12 else "****"
            print_result(True, f"{description}: {display_value}")
        else:
            print_result(False, f"{description} - NOT SET")
            all_set = False

    return all_set


def test_api_connection():
    """Test 2: Test API connectivity (without audio)."""
    print_header("TEST 2: API Connection Test")

    try:
        import requests

        endpoint = os.getenv("AZURE_TRANSCRIBE_ENDPOINT", "").rstrip('/')
        api_key = os.getenv("AZURE_TRANSCRIBE_API_KEY", "")
        deployment = os.getenv("AZURE_TRANSCRIBE_DEPLOYMENT", "")
        api_version = os.getenv("AZURE_TRANSCRIBE_API_VERSION", "")

        if not all([endpoint, api_key, deployment, api_version]):
            print_result(False, "Missing required environment variables")
            return False

        # Build URL
        url = f"{endpoint}/openai/deployments/{deployment}/audio/transcriptions?api-version={api_version}"
        print(f"  API URL: {url[:60]}...")

        # Test with empty request (will fail but confirms connectivity)
        headers = {"api-key": api_key}

        response = requests.post(
            url,
            headers=headers,
            data={"response_format": "json"},
            timeout=10
        )

        # We expect 400 (bad request - no file) or 415 (unsupported media type)
        # These errors confirm the API is reachable
        if response.status_code in [400, 415, 422]:
            print_result(True, f"API is reachable (got expected error: {response.status_code})")
            return True
        elif response.status_code == 401:
            print_result(False, "Authentication failed - check API key")
            return False
        elif response.status_code == 404:
            print_result(False, "Deployment not found - check deployment name")
            return False
        else:
            print_result(True, f"API responded with status: {response.status_code}")
            return True

    except requests.exceptions.ConnectionError:
        print_result(False, "Cannot connect to API endpoint")
        return False
    except Exception as e:
        print_result(False, f"Connection test failed: {e}")
        return False


def test_transcription_with_file(audio_path: str):
    """Test 3: Test actual transcription with audio file."""
    print_header("TEST 3: Audio Transcription")

    if not os.path.exists(audio_path):
        print_result(False, f"Audio file not found: {audio_path}")
        return False

    try:
        from processing.transcriber_v2 import TranscriberV2

        file_size = os.path.getsize(audio_path) / 1024
        print(f"  Audio file: {audio_path}")
        print(f"  File size: {file_size:.2f} KB")

        print("\n  Calling Azure Transcription API...")
        print("  (This may take 10-30 seconds depending on audio length)")

        transcriber = TranscriberV2()

        # Get raw response for debugging
        print("\n  --- Raw API Response ---")
        raw_response = transcriber.get_raw_transcription(audio_path)

        if raw_response:
            print(f"  Response keys: {list(raw_response.keys())}")

            # Check for segments
            segments = raw_response.get("segments", [])
            print(f"  Number of segments: {len(segments)}")

            if segments:
                print("\n  First 3 segments:")
                for i, seg in enumerate(segments[:3]):
                    speaker = seg.get("speaker", "unknown")
                    text = seg.get("text", "")[:50]
                    print(f"    [{speaker}]: {text}...")

            # Plain text
            plain_text = raw_response.get("text", "")
            print(f"\n  Plain text length: {len(plain_text)} chars")

        # Get formatted transcript
        print("\n  --- Formatted Transcript ---")
        transcript = transcriber.transcribe(audio_path)

        if transcript:
            print(f"\n{transcript}\n")
            print("-" * 40)
            print_result(True, f"Transcription successful ({len(transcript)} characters)")
            return True
        else:
            print_result(False, "Empty transcription result")
            return False

    except Exception as e:
        print_result(False, f"Transcription failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def create_test_audio():
    """Create a simple test audio file for testing (if pydub available)."""
    try:
        from pydub import AudioSegment
        from pydub.generators import Sine

        test_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "test_audio.wav"
        )

        # Generate 3 seconds of silence (test file)
        audio = AudioSegment.silent(duration=3000)
        audio.export(test_path, format="wav")

        print(f"  Created test audio: {test_path}")
        return test_path

    except ImportError:
        return None


def main():
    """Run transcription tests."""
    print("\n" + "=" * 60)
    print("       AIMScribe - Transcription API Test")
    print("=" * 60)

    # Check for audio file argument
    audio_path = None
    if len(sys.argv) > 1:
        audio_path = sys.argv[1]
        print(f"\n  Using audio file: {audio_path}")

    results = []

    # Test 1: Environment
    results.append(("Environment Variables", test_environment()))

    # Test 2: API Connection
    results.append(("API Connection", test_api_connection()))

    # Test 3: Transcription (if audio file provided)
    if audio_path and os.path.exists(audio_path):
        results.append(("Audio Transcription", test_transcription_with_file(audio_path)))
    else:
        # Look for test audio in tests directory
        test_audio = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "test_audio.wav"
        )
        if os.path.exists(test_audio):
            results.append(("Audio Transcription", test_transcription_with_file(test_audio)))
        else:
            print_header("TEST 3: Audio Transcription")
            print("  ⚠️  SKIPPED: No audio file provided")
            print(f"  To test transcription, run:")
            print(f"    python tests/test_transcription.py path/to/audio.wav")
            print(f"\n  Or place a test file at:")
            print(f"    tests/test_audio.wav")
            results.append(("Audio Transcription", None))

    # Summary
    print_header("TEST SUMMARY")

    passed = 0
    failed = 0
    skipped = 0

    for test_name, result in results:
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

    if failed == 0 and passed > 0:
        print("\n  🎉 Transcription API is configured correctly!")
    elif failed > 0:
        print("\n  ⚠️  Some tests failed. Check the configuration above.")

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
