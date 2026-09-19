"""
AIMScribe AI Backend - Transcriber V4 Module
Uses Azure GPT-4o-Transcribe-Diarize via Audio Transcriptions API.

APPROACH:
  - Sends .wav audio as multipart form data to /audio/transcriptions endpoint
  - Uses 'diarized_json' response format with chunking_strategy='auto'
  - API returns segments array with speaker labels ("A", "B", "C", ...)
  - Merges consecutive segments from the same speaker
  - Maps speaker letters to numbered labels: [Speaker_01], [Speaker_02], ...
  - Does NOT assume who is doctor/patient — NER extraction handles that from context

Endpoint: /openai/deployments/{deployment}/audio/transcriptions
API Version: 2025-03-01-preview
"""

import logging
import json
import requests
from typing import Optional, Dict, Any, List
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


class TranscriberV4:
    """
    Bengali Medical Transcriber using Azure Audio Transcriptions API
    with GPT-4o-Transcribe-Diarize model.

    Uses 'diarized_json' response format — the API returns structured segments
    with speaker labels ("A", "B", etc.). These are mapped to numbered labels
    [Speaker_01], [Speaker_02] without assuming roles.
    GPT 5.2 NER extraction determines who is doctor/patient from context.
    """

    # Supported audio formats
    CONTENT_TYPES = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/m4a",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
    }

    def __init__(self):
        """Initialize Azure Audio Transcription client."""
        self.endpoint = settings.azure_transcribe_endpoint.rstrip('/')
        self.api_key = settings.azure_transcribe_api_key
        self.deployment = settings.azure_transcribe_deployment
        self.api_version = settings.azure_transcribe_api_version

        # Build the API URL
        self.api_url = (
            f"{self.endpoint}/openai/deployments/{self.deployment}"
            f"/audio/transcriptions?api-version={self.api_version}"
        )

        logger.info(f"TranscriberV4 initialized")
        logger.info(f"  Deployment: {self.deployment}")
        logger.info(f"  API: Audio Transcriptions (diarized_json format)")
        logger.info(f"  API Version: {self.api_version}")

    def transcribe(self, audio_path: str, session_id: str = None) -> str:
        """
        Transcribe Bengali audio with speaker diarization.

        Args:
            audio_path: Path to the audio file (.wav, .mp3, etc.)
            session_id: Optional session ID for logging

        Returns:
            Bengali transcript with [Speaker_01], [Speaker_02] labels
        """
        try:
            logger.info(f"Starting transcription for: {audio_path}")
            if session_id:
                logger.info(f"  Session: {session_id}")

            # Validate audio file
            path = Path(audio_path)
            if not path.exists():
                logger.error(f"Audio file not found: {audio_path}")
                return ""

            if path.stat().st_size == 0:
                logger.warning(f"Audio file is empty: {audio_path}")
                return ""

            file_size_kb = path.stat().st_size / 1024
            logger.info(f"Audio file size: {file_size_kb:.2f} KB")

            # Check supported format
            extension = path.suffix.lower()
            if extension not in self.CONTENT_TYPES:
                logger.error(f"Unsupported audio format: {extension}")
                return ""

            # Call the Audio Transcriptions API
            api_response = self._call_api(audio_path)

            if not api_response:
                logger.warning("Empty transcription response")
                return ""

            # Format diarized segments into readable transcript
            segments = api_response.get("segments", [])

            if segments:
                transcript = self._format_segments(segments)
            else:
                # Fallback: if no segments, return plain text
                logger.warning("No segments in response — using plain text fallback")
                transcript = api_response.get("text", "")

            if transcript:
                logger.info(f"Transcription completed: {len(transcript)} characters")
                logger.debug(f"Transcript preview: {transcript[:300]}...")
            else:
                logger.warning("Empty transcript after formatting")

            return transcript

        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            raise

    def _call_api(self, audio_path: str) -> Optional[Dict[str, Any]]:
        """
        Call Azure Audio Transcriptions API with diarized_json format.

        Args:
            audio_path: Path to audio file

        Returns:
            Full API response as dictionary (contains "text" and "segments")
        """
        path = Path(audio_path)
        content_type = self.CONTENT_TYPES.get(path.suffix.lower(), "audio/wav")

        headers = {
            "api-key": self.api_key,
        }

        with open(audio_path, "rb") as audio_file:
            files = {
                "file": (path.name, audio_file, content_type)
            }

            data = {
                "model": self.deployment,
                "response_format": "diarized_json",  # Returns segments with speaker labels
                "chunking_strategy": "auto",  # Required for diarization models
                "language": "bn",  # Bengali — re-enabled (500 error was from missing chunking_strategy, not language)
                # "response_format": "json",  # Old: plain json without speaker segments
            }

            logger.debug(f"Calling API: {self.api_url}")

            response = requests.post(
                self.api_url,
                headers=headers,
                files=files,
                data=data,
                timeout=120
            )

        if response.status_code != 200:
            logger.error(f"API Error {response.status_code}: {response.text}")
            response.raise_for_status()

        result = response.json()

        logger.debug(f"API response keys: {list(result.keys())}")
        logger.debug(f"Segments count: {len(result.get('segments', []))}")

        return result

    def _format_segments(self, segments: List[Dict[str, Any]]) -> str:
        """
        Format diarized segments into readable transcript with numbered speaker labels.

        API returns segments like:
            [
                {"text": " আজকে কি সমস্যা", "speaker": "A", "start": 0.0, "end": 2.5},
                {"text": " নিয়ে আসছেন?", "speaker": "A", "start": 2.5, "end": 4.0},
                {"text": " তিন দিন ধরে জ্বর।", "speaker": "B", "start": 4.5, "end": 6.0},
            ]

        Output:
            [Speaker_01]: আজকে কি সমস্যা নিয়ে আসছেন?
            [Speaker_02]: তিন দিন ধরে জ্বর।

        Args:
            segments: List of segment dicts from API response

        Returns:
            Formatted transcript with [Speaker_XX] labels
        """
        if not segments:
            return ""

        # Map speaker letters (A, B, C...) to numbered labels as they appear
        speaker_number_map = {}
        next_speaker_num = 1

        # Merge consecutive segments from the same speaker
        merged_lines = []
        current_speaker = None
        current_texts = []

        for segment in segments:
            speaker = segment.get("speaker", "unknown")
            text = segment.get("text", "").strip()

            if not text:
                continue

            # Assign numbered label to new speakers in order of appearance
            if speaker not in speaker_number_map:
                speaker_number_map[speaker] = f"Speaker_{next_speaker_num:02d}"
                next_speaker_num += 1
                logger.debug(f"New speaker detected: {speaker} → {speaker_number_map[speaker]}")

            if speaker == current_speaker:
                # Same speaker — merge text
                current_texts.append(text)
            else:
                # Different speaker — save previous and start new
                if current_speaker is not None and current_texts:
                    label = speaker_number_map[current_speaker]
                    merged_text = " ".join(current_texts).strip()
                    merged_lines.append(f"[{label}]: {merged_text}")

                current_speaker = speaker
                current_texts = [text]

        # Don't forget the last speaker
        if current_speaker is not None and current_texts:
            label = speaker_number_map[current_speaker]
            merged_text = " ".join(current_texts).strip()
            merged_lines.append(f"[{label}]: {merged_text}")

        transcript = "\n".join(merged_lines)

        logger.info(f"Formatted {len(segments)} segments into {len(merged_lines)} speaker turns")
        logger.info(f"Speakers detected: {list(speaker_number_map.keys())} → {list(speaker_number_map.values())}")

        return transcript

    def transcribe_with_context(
        self,
        audio_path: str,
        previous_transcript: str = None,
        session_id: str = None
    ) -> str:
        """
        Transcribe audio. Context parameter exists for API compatibility
        but the Audio Transcriptions API does not accept context.

        Speaker continuity across clips is handled by the worker
        when it concatenates clip transcripts.

        Args:
            audio_path: Path to audio file
            previous_transcript: Previous transcript (logged only, not sent to API)
            session_id: Optional session ID

        Returns:
            Transcribed text with [Speaker_XX] labels
        """
        if previous_transcript:
            logger.debug(
                f"Previous context available (last 100 chars): "
                f"{previous_transcript[-100:]}"
            )

        return self.transcribe(audio_path, session_id)

    def get_raw_response(self, audio_path: str) -> Optional[Dict[str, Any]]:
        """
        Get raw API response for debugging — see exactly what
        the API returns before any formatting.

        Args:
            audio_path: Path to audio file

        Returns:
            Raw API response dictionary
        """
        return self._call_api(audio_path)
