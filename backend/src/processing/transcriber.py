"""
AIMScribe AI Backend - Transcriber Module
Handles audio transcription using Azure OpenAI.
"""

import logging
import json
import os
from typing import Optional

from openai import AzureOpenAI
from config import settings
from prompts.loader import load_prompt

logger = logging.getLogger(__name__)


class Transcriber:
    """
    Handles audio transcription and formatting.
    """
    
    def __init__(self):
        # Client for Audio -> Text (Whisper)
        self.asr_client = AzureOpenAI(
            azure_endpoint=settings.azure_transcribe_endpoint,
            api_key=settings.azure_transcribe_api_key,
            api_version=settings.azure_api_version
        )
        
        # Client for Formatting/Refinement (GPT-4o)
        self.chat_client = AzureOpenAI(
            azure_endpoint=settings.azure_ner_endpoint,
            api_key=settings.azure_ner_api_key,
            api_version=settings.azure_api_version
        )
        
    def transcribe(self, audio_path: str, session_id: str = None) -> str:
        """
        Transcribe audio file to formatted text.
        
        Process:
        1. Raw ASR (Audio -> Text) using Azure Whisper
        2. Formatting/Labeling using Transcription Agent (GPT-4o)
        """
        try:
            # Step 1: Raw Transcription (Whisper)
            logger.info(f"Starting raw transcription for: {audio_path}")
            
            with open(audio_path, "rb") as audio_file:
                result = self.asr_client.audio.transcriptions.create(
                    model=settings.azure_transcribe_deployment,
                    file=audio_file,
                    language="bn",  # Bengali
                    response_format="text"
                )
            
            raw_transcript = result if isinstance(result, str) else result.text
            logger.debug(f"Raw transcript: {raw_transcript[:100]}...")
            
            # Step 2: Formatting with Transcription Agent via Chat Completion
            # Note: If the raw transcript is empty, return empty
            if not raw_transcript.strip():
                return ""
                
            return self._format_transcript(raw_transcript)
            
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            raise

    def _format_transcript(self, raw_text: str) -> str:
        """Format raw transcript using the Transcription Agent prompt."""
        try:
            # Load system prompt
            system_prompt = load_prompt("agents", "transcription_agent")
            
            # Call GPT-4 (or 4o)
            response = self.chat_client.chat.completions.create(
                model=settings.azure_ner_deployment,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Please transcribe/format this raw text:\n\n{raw_text}"}
                ],
                temperature=0.3
            )
            
            formatted_text = response.choices[0].message.content
            return formatted_text
            
        except Exception as e:
            logger.warning(f"Transcript formatting failed, returning raw text: {e}")
            return raw_text
