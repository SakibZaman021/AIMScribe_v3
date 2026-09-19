"""
AIMScribe AI Backend - Transcriber V3 Module
Uses Azure GPT-5.2-chat via Chat Completions API for Bengali audio transcription.

APPROACH: Multimodal LLM (like Gemini) — sends audio to a chat model that:
  1. Listens to the audio
  2. Understands the medical conversation context
  3. Transcribes in Bengali
  4. Labels speakers semantically as [ডাক্তার], [রোগী], [রোগীর সাথী]

Endpoint: /openai/deployments/{deployment}/chat/completions
API Version: 2025-01-01-preview
"""

import base64
import logging
import json
from typing import Optional, Dict, Any
from pathlib import Path

from openai import AzureOpenAI
from config import settings
from prompts.loader import get_prompt_loader

logger = logging.getLogger(__name__)


class TranscriberV3:
    """
    Bengali Medical Transcriber using Azure Chat Completions API with audio input.

    Unlike V2 (Audio Transcriptions API), this sends audio to a multimodal LLM
    that understands context and labels speakers semantically:
      - [ডাক্তার] (Doctor)
      - [রোগী] (Patient)
      - [রোগীর সাথী] (Patient's companion)
    """

    # Supported audio formats and their MIME types
    SUPPORTED_FORMATS = {
        ".wav": "wav",
        ".mp3": "mp3",
        ".m4a": "m4a",
        ".webm": "webm",
        ".ogg": "ogg",
    }

    def __init__(self):
        """Initialize Azure Chat Completions client for transcription."""
        self.client = AzureOpenAI(
            azure_endpoint=settings.azure_transcribe_endpoint.rstrip('/'),
            api_key=settings.azure_transcribe_api_key,
            api_version=settings.azure_transcribe_api_version,
        )
        self.deployment = settings.azure_transcribe_deployment

        # Load system prompt from template
        try:
            loader = get_prompt_loader()
            self.system_prompt = loader.load("agents", "transcription_agent")
        except Exception as e:
            logger.warning(f"Could not load prompt template, using default: {e}")
            self.system_prompt = self._default_system_prompt()

        logger.info(f"TranscriberV3 initialized")
        logger.info(f"  Deployment: {self.deployment}")
        logger.info(f"  API: Chat Completions (multimodal audio)")

    def transcribe(self, audio_path: str, session_id: str = None) -> str:
        """
        Transcribe Bengali audio with semantic speaker labeling.

        Args:
            audio_path: Path to the audio file (.wav, .mp3, etc.)
            session_id: Optional session ID for logging

        Returns:
            Bengali transcript with [ডাক্তার], [রোগী] speaker labels
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
            if extension not in self.SUPPORTED_FORMATS:
                logger.error(f"Unsupported audio format: {extension}")
                return ""

            audio_format = self.SUPPORTED_FORMATS[extension]

            # Encode audio to base64
            with open(audio_path, "rb") as f:
                audio_base64 = base64.b64encode(f.read()).decode("utf-8")

            logger.info(f"Audio encoded, sending to {self.deployment}...")

            # Call Chat Completions API with audio input
            response = self.client.chat.completions.create(
                model=self.deployment,
                messages=[
                    {
                        "role": "system",
                        "content": self.system_prompt
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "এই বাংলা মেডিকেল কথোপকথন ট্রান্সক্রাইব করুন। প্রতিটি বক্তব্য সঠিক স্পিকার লেবেল দিয়ে শুরু করুন।"
                            },
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": audio_base64,
                                    "format": audio_format
                                }
                            }
                        ]
                    }
                ],
                temperature=0.1,
                max_tokens=4000
            )

            transcript = response.choices[0].message.content
            transcript = self._clean_transcript(transcript)

            if transcript:
                logger.info(f"Transcription completed: {len(transcript)} characters")
                logger.debug(f"Transcript preview: {transcript[:200]}...")
            else:
                logger.warning("Empty transcript received")

            return transcript

        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            raise

    def transcribe_with_context(
        self,
        audio_path: str,
        previous_transcript: str = None,
        session_id: str = None
    ) -> str:
        """
        Transcribe audio with context from previous clips.

        Provides the previous transcript to the LLM so it can maintain
        speaker continuity across clips (e.g., if clip 1 ends with the doctor
        speaking, clip 2 should continue from that context).

        Args:
            audio_path: Path to audio file
            previous_transcript: Transcript from previous clips
            session_id: Optional session ID

        Returns:
            Bengali transcript with speaker labels
        """
        if not previous_transcript:
            return self.transcribe(audio_path, session_id)

        try:
            path = Path(audio_path)
            if not path.exists() or path.stat().st_size == 0:
                return ""

            extension = path.suffix.lower()
            audio_format = self.SUPPORTED_FORMATS.get(extension, "wav")

            with open(audio_path, "rb") as f:
                audio_base64 = base64.b64encode(f.read()).decode("utf-8")

            # Use last 500 chars of previous transcript for context
            context_snippet = previous_transcript[-500:] if len(previous_transcript) > 500 else previous_transcript

            logger.info(f"Transcribing with context from previous clips...")

            response = self.client.chat.completions.create(
                model=self.deployment,
                messages=[
                    {
                        "role": "system",
                        "content": self.system_prompt
                    },
                    {
                        "role": "user",
                        "content": f"এটি একটি চলমান কথোপকথনের আগের অংশ:\n\n{context_snippet}"
                    },
                    {
                        "role": "assistant",
                        "content": "বুঝেছি। আমি আগের কথোপকথনের ধারাবাহিকতা বজায় রেখে পরবর্তী অংশ ট্রান্সক্রাইব করবো।"
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "এই পরবর্তী অডিও ক্লিপটি ট্রান্সক্রাইব করুন। আগের কথোপকথনের স্পিকার ধারাবাহিকতা বজায় রাখুন।"
                            },
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": audio_base64,
                                    "format": audio_format
                                }
                            }
                        ]
                    }
                ],
                temperature=0.1,
                max_tokens=4000
            )

            transcript = response.choices[0].message.content
            transcript = self._clean_transcript(transcript)

            if transcript:
                logger.info(f"Context-aware transcription completed: {len(transcript)} characters")

            return transcript

        except Exception as e:
            logger.error(f"Context-aware transcription failed: {e}")
            raise

    def _clean_transcript(self, text: str) -> str:
        """
        Clean transcript output — remove markdown fencing if the model wraps it.

        Args:
            text: Raw transcript from LLM

        Returns:
            Cleaned transcript
        """
        if not text:
            return ""

        text = text.strip()

        # Remove markdown code blocks if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```...) and last line (```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        return text.strip()

    def _default_system_prompt(self) -> str:
        """Fallback system prompt if template file is unavailable."""
        return """You are an expert medical transcription system for Bengali (বাংলা) doctor-patient conversations.

## TASK
Transcribe the audio accurately in Bengali with speaker labels.

## SPEAKER LABELS (use these EXACTLY)
- [ডাক্তার]: Doctor's speech
- [রোগী]: Patient's speech
- [রোগীর সাথী]: Patient's companion's speech

## RULES
1. Every utterance MUST start with a speaker label
2. Transcribe in Bengali script only
3. Medical terms in Bengali script (BP → বিপি, ECG → ইসিজি)
4. Use Bengali punctuation (। , ? !)
5. Do NOT hallucinate — only transcribe what you hear
6. Do NOT skip any part of the conversation
7. Include hesitations in Bengali (উম, আ, এই)

## EXAMPLE OUTPUT
[ডাক্তার]: আজকে কি সমস্যা নিয়ে আসছেন?
[রোগী]: তিন দিন ধরে জ্বর। গায়ে অনেক ব্যথা।
[ডাক্তার]: জ্বর কত আসছে?
[রোগী]: ১০১-১০২ এর মতো।"""
