"""
CMED Webhook Sender
Pushes NER results to CMED system after extraction.
"""
import hmac
import hashlib
import os
import time
import json
import logging
import asyncio
from typing import Optional, Dict, Any
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

# Shared with CMED, which rejects any webhook whose signature does not verify.
# Must match AIMSCRIBE_WEBHOOK_SECRET in cmed-web. Empty means unsigned webhooks,
# which CMED drops - so the failure is visible rather than silent.
WEBHOOK_SECRET = os.getenv("AIMSCRIBE_WEBHOOK_SECRET", "")

if not WEBHOOK_SECRET:
    logger.critical(
        "AIMSCRIBE_WEBHOOK_SECRET is not set. NER webhooks will be signed with "
        "an empty key and CMED will reject them, so prescription fields will "
        "never populate.")

# Retry configuration
MAX_RETRIES = 6
RETRY_DELAYS = [0, 5, 30, 120, 600, 3600]  # seconds: immediate, 5s, 30s, 2m, 10m, 1h


@dataclass
class WebhookResult:
    """Result of webhook call"""
    success: bool
    status_code: int = 0
    attempts: int = 0
    error: Optional[str] = None


def generate_signature(payload: str, timestamp: str, secret: str = WEBHOOK_SECRET) -> str:
    """Generate HMAC-SHA256 signature for webhook payload."""
    message = f"{timestamp}.{payload}"
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"sha256={signature}"


async def send_ner_webhook(
    webhook_url: str,
    session_id: str,
    patient_id: str,
    doctor_id: str,
    hospital_id: str,
    clip_number: int,
    total_clips: int,
    is_final: bool,
    ner_data: Dict[str, Any],
    version: int,
    transcript: str = None
) -> WebhookResult:
    """
    Send NER results to CMED webhook.

    Args:
        webhook_url: CMED webhook endpoint
        session_id: Session ID
        patient_id: Patient ID
        doctor_id: Doctor ID
        hospital_id: Hospital ID
        clip_number: Current clip number
        total_clips: Total clips processed
        is_final: Whether this is the final NER
        ner_data: NER extraction results
        version: NER version number
        transcript: Full transcript (optional, for final event)

    Returns:
        WebhookResult with success status
    """
    if not webhook_url:
        logger.debug("No webhook URL configured, skipping")
        return WebhookResult(success=True, attempts=0)

    # Build payload
    event_type = "ner.final" if is_final else "ner.extracted"
    timestamp = str(int(time.time()))

    payload = {
        "event": event_type,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session": {
            "id": session_id,
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "hospital_id": hospital_id
        },
        "clip": {
            "number": clip_number,
            "total_clips": total_clips,
            "is_final": is_final
        },
        "ner": {
            "version": version,
            "chief_complaints": ner_data.get("Chief Complaints (English)", []),
            # Drug History is nested inside Examination
            "drug_history": ner_data.get("Examination (English)", {}).get("Drug History (English)", []),
            # On Examination (O/E) is nested inside Examination
            "on_examination": ner_data.get("Examination (English)", {}).get("O/E (English)", {}),
            # Systemic Examination (S/E) is nested inside Examination
            "systemic_examination": ner_data.get("Examination (English)", {}).get("S/E (English)", {}),
            # Additional Notes is nested inside Examination
            "additional_notes": ner_data.get("Examination (English)", {}).get("Additional Notes (English)", []),
            "investigations": ner_data.get("Investigations (English)", []),
            "diagnosis": ner_data.get("Diagnosis (English)", []),
            "medications": ner_data.get("Medications", []),
            "advice": ner_data.get("Advice (Bengali)", []),
            "follow_up": ner_data.get("Follow Up (Bengali)", {}),
            "health_screening": ner_data.get("Health Screening", {})
        }
    }

    # Include patient info if available
    patient_info = ner_data.get("Patient Info (English)", {})
    if patient_info:
        payload["ner"]["patient_info"] = {
            "name": patient_info.get("Name (English)"),
            "age": patient_info.get("Age (English)"),
            "gender": patient_info.get("Gender (English)")
        }

    # Include transcript for final event
    if is_final and transcript:
        payload["transcript"] = transcript

    payload_json = json.dumps(payload, ensure_ascii=False)

    # Generate signature
    signature = generate_signature(payload_json, timestamp)

    # Headers
    headers = {
        "Content-Type": "application/json",
        "X-AIMScribe-Signature": signature,
        "X-AIMScribe-Timestamp": timestamp,
        "X-AIMScribe-Event": event_type
    }

    # Send with retry
    async with aiohttp.ClientSession() as session:
        for attempt in range(MAX_RETRIES):
            try:
                delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
                if delay > 0:
                    logger.info(f"Webhook retry {attempt + 1}/{MAX_RETRIES} in {delay}s")
                    await asyncio.sleep(delay)

                async with session.post(
                    webhook_url,
                    data=payload_json,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        logger.info(f"Webhook sent successfully to {webhook_url}")
                        return WebhookResult(
                            success=True,
                            status_code=response.status,
                            attempts=attempt + 1
                        )
                    elif response.status >= 400 and response.status < 500:
                        # Client error - don't retry
                        error_text = await response.text()
                        logger.error(f"Webhook client error {response.status}: {error_text}")
                        return WebhookResult(
                            success=False,
                            status_code=response.status,
                            attempts=attempt + 1,
                            error=error_text
                        )
                    else:
                        # Server error - retry
                        logger.warning(f"Webhook server error {response.status}, will retry")

            except asyncio.TimeoutError:
                logger.warning(f"Webhook timeout (attempt {attempt + 1})")
            except aiohttp.ClientError as e:
                logger.warning(f"Webhook connection error: {e}")
            except Exception as e:
                logger.error(f"Webhook unexpected error: {e}")

    # All retries exhausted
    logger.error(f"Webhook failed after {MAX_RETRIES} attempts")
    return WebhookResult(
        success=False,
        attempts=MAX_RETRIES,
        error="Max retries exhausted"
    )


async def send_status_webhook(
    webhook_url: str,
    session_id: str,
    patient_id: str,
    status: str,
    message: str = None
) -> WebhookResult:
    """
    Send status update to CMED webhook.

    Args:
        webhook_url: CMED status webhook endpoint
        session_id: Session ID
        patient_id: Patient ID
        status: Status string (e.g., "recording_started", "processing", "completed")
        message: Optional message

    Returns:
        WebhookResult with success status
    """
    if not webhook_url:
        return WebhookResult(success=True, attempts=0)

    timestamp = str(int(time.time()))

    payload = {
        "event": f"session.{status}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session": {
            "id": session_id,
            "patient_id": patient_id
        },
        "status": status,
        "message": message
    }

    payload_json = json.dumps(payload, ensure_ascii=False)
    signature = generate_signature(payload_json, timestamp)

    headers = {
        "Content-Type": "application/json",
        "X-AIMScribe-Signature": signature,
        "X-AIMScribe-Timestamp": timestamp,
        "X-AIMScribe-Event": f"session.{status}"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                data=payload_json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                return WebhookResult(
                    success=response.status == 200,
                    status_code=response.status,
                    attempts=1
                )
    except Exception as e:
        logger.warning(f"Status webhook failed: {e}")
        return WebhookResult(success=False, attempts=1, error=str(e))
