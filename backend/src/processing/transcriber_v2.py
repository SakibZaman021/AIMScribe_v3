"""
AIMScribe AI Backend - Transcriber V2 Module
Uses Azure GPT-4o-Transcribe with Diarization for Bengali audio.

IMPORTANT: This uses the Azure Audio Transcriptions API with speaker diarization.
- Endpoint: /openai/deployments/{deployment}/audio/transcriptions
- API Version: 2025-03-01-preview
- Features: Native speaker diarization, Bengali support
"""

import logging
import requests
import json
from typing import Optional, Dict, Any, List
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


class TranscriberV2:
    """
    Bengali Medical Transcriber using Azure GPT-4o-Transcribe with Diarization.

    Features:
    - Native speaker diarization (automatic speaker detection)
    - Bengali language transcription
    - Medical terminology support
    - Speaker labeling: [ডাক্তার], [রোগী], [রোগীর সাথী]
    """

    # Speaker label mapping
    SPEAKER_LABELS = {
        "speaker_0": "[ডাক্তার]",      # First speaker = Doctor
        "speaker_1": "[রোগী]",          # Second speaker = Patient
        "speaker_2": "[রোগীর সাথী]",    # Third speaker = Patient's companion
        "speaker_3": "[অন্যান্য]",      # Additional speakers
    }

    def __init__(self):
        """Initialize Azure Audio Transcription client."""
        self.endpoint = settings.azure_transcribe_endpoint.rstrip('/')
        self.api_key = settings.azure_transcribe_api_key
        self.deployment = settings.azure_transcribe_deployment
        self.api_version = settings.azure_transcribe_api_version

        # Build the full API URL
        self.api_url = (
            f"{self.endpoint}/openai/deployments/{self.deployment}"
            f"/audio/transcriptions?api-version={self.api_version}"
        )

        logger.info(f"TranscriberV2 initialized")
        logger.info(f"  Deployment: {self.deployment}")
        logger.info(f"  API Version: {self.api_version}")

    def transcribe(self, audio_path: str, session_id: str = None) -> str:
        """
        Transcribe Bengali audio with speaker diarization.

        Args:
            audio_path: Path to the audio file (.wav, .mp3, etc.)
            session_id: Optional session ID for logging

        Returns:
            Formatted transcript with speaker labels in Bengali format
        """
        try:
            logger.info(f"Starting transcription for: {audio_path}")

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

            # Call Azure Audio Transcriptions API
            response_data = self._call_transcription_api(audio_path)

            if not response_data:
                logger.warning("Empty transcription response")
                return ""

            # Format the response with speaker labels
            transcript = self._format_diarized_transcript(response_data)

            if transcript:
                logger.info(f"Transcription completed: {len(transcript)} characters")
                logger.debug(f"Transcript preview: {transcript[:200]}...")
            else:
                logger.warning("Empty formatted transcript")

            return transcript

        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            raise

    def _call_transcription_api(self, audio_path: str) -> Optional[Dict[str, Any]]:
        """
        Call Azure Audio Transcriptions API.

        Args:
            audio_path: Path to audio file

        Returns:
            API response as dictionary
        """
        try:
            # Prepare headers
            headers = {
                "api-key": self.api_key,
            }

            # Determine content type based on file extension
            path = Path(audio_path)
            extension = path.suffix.lower()
            content_types = {
                ".wav": "audio/wav",
                ".mp3": "audio/mpeg",
                ".m4a": "audio/m4a",
                ".webm": "audio/webm",
                ".ogg": "audio/ogg",
            }
            content_type = content_types.get(extension, "audio/wav")

            # Prepare multipart form data
            with open(audio_path, "rb") as audio_file:
                files = {
                    "file": (path.name, audio_file, content_type)
                }

                # Request parameters for diarization
                data = {
                    "response_format": "verbose_json",  # Get detailed response with timestamps
                    "language": "bn",  # Bengali language code
                    "timestamp_granularities": "segment",  # Get segment-level timestamps
                }

                logger.debug(f"Calling API: {self.api_url}")

                # Make the API call
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=120  # 2 minute timeout for audio processing
                )

            # Check for errors
            if response.status_code != 200:
                logger.error(f"API Error {response.status_code}: {response.text}")
                response.raise_for_status()

            # Parse response
            result = response.json()
            logger.debug(f"API Response keys: {result.keys()}")

            return result

        except requests.exceptions.Timeout:
            logger.error("API call timed out")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse API response: {e}")
            raise

    def _format_diarized_transcript(self, response_data: Dict[str, Any]) -> str:
        """
        Format the API response into a readable transcript with speaker labels.

        The API returns segments with speaker information. We format this into:
        [ডাক্তার]: Text spoken by doctor
        [রোগী]: Text spoken by patient

        Args:
            response_data: API response dictionary

        Returns:
            Formatted transcript string
        """
        try:
            # Check if we have segments with speaker info
            segments = response_data.get("segments", [])

            if not segments:
                # Fallback: return plain text if no segments
                return response_data.get("text", "")

            # Build formatted transcript
            formatted_lines = []
            current_speaker = None
            current_text = []

            for segment in segments:
                # Get speaker ID (e.g., "speaker_0", "speaker_1")
                speaker_id = segment.get("speaker", "speaker_0")
                text = segment.get("text", "").strip()

                if not text:
                    continue

                # Get Bengali label for speaker
                speaker_label = self.SPEAKER_LABELS.get(
                    speaker_id,
                    f"[বক্তা {speaker_id[-1]}]"  # Fallback: "Speaker X" in Bengali
                )

                # Check if speaker changed
                if speaker_id != current_speaker:
                    # Save previous speaker's text
                    if current_speaker is not None and current_text:
                        prev_label = self.SPEAKER_LABELS.get(
                            current_speaker,
                            f"[বক্তা {current_speaker[-1]}]"
                        )
                        formatted_lines.append(f"{prev_label}: {' '.join(current_text)}")

                    # Start new speaker
                    current_speaker = speaker_id
                    current_text = [text]
                else:
                    # Same speaker, append text
                    current_text.append(text)

            # Don't forget the last speaker's text
            if current_speaker is not None and current_text:
                speaker_label = self.SPEAKER_LABELS.get(
                    current_speaker,
                    f"[বক্তা {current_speaker[-1]}]"
                )
                formatted_lines.append(f"{speaker_label}: {' '.join(current_text)}")

            # Join all lines
            transcript = "\n".join(formatted_lines)

            # If no speaker segments found, return plain text
            if not transcript:
                transcript = response_data.get("text", "")

            return transcript

        except Exception as e:
            logger.warning(f"Error formatting transcript: {e}")
            # Fallback to plain text
            return response_data.get("text", "")

    def _clean_transcript(self, text: str) -> str:
        """
        Clean transcript output.

        Args:
            text: Raw transcript

        Returns:
            Cleaned transcript
        """
        if not text:
            return ""

        # Strip whitespace
        text = text.strip()

        # Remove any markdown formatting if present
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 2:
                text = parts[1]
                if "\n" in text:
                    text = text.split("\n", 1)[1]

        return text.strip()

    def transcribe_with_context(
        self,
        audio_path: str,
        previous_transcript: str = None,
        session_id: str = None
    ) -> str:
        """
        Transcribe audio (context is handled by speaker continuity in diarization).

        Note: The Azure diarization API handles speaker identification automatically.
        Previous context is mainly for logging/debugging purposes.

        Args:
            audio_path: Path to audio file
            previous_transcript: Previous transcript (for logging)
            session_id: Optional session ID

        Returns:
            Transcribed text with speaker labels
        """
        if previous_transcript:
            logger.debug(f"Context available from previous clip (last 100 chars): {previous_transcript[-100:]}")

        return self.transcribe(audio_path, session_id)

    def get_raw_transcription(self, audio_path: str) -> Dict[str, Any]:
        """
        Get raw API response for debugging/analysis.

        Args:
            audio_path: Path to audio file

        Returns:
            Raw API response dictionary
        """
        return self._call_transcription_api(audio_path)
