"""The device log's record of what a PC sent (043): rules that need no database."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from starlette.requests import Request  # noqa: E402

import device_log  # noqa: E402


def _request(path, headers):
    scope = {"type": "http", "method": "POST", "path": path, "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    return Request(scope)


def test_a_signed_request_says_how_far_the_pcs_clock_is():
    now = float(int(time.time()))
    body = json.dumps({"session_id": "S1", "seq_no": 4, "patient_ref": "P-77",
                       "audio": {"sample_rate": 44100, "channels": 1}}).encode()
    request = _request("/api/v2/segment/commit", {
        "User-Agent": "AIMScribe/3.8.3", "X-Device-Token": "tok-1234567890",
        "X-Device-Signature": "abc", "X-Device-Timestamp": str(int(now + 312)),
        "Content-Type": "application/json", "Content-Length": str(len(body))})
    sent = device_log.what_was_sent(request, body, now, known_pc=True)
    assert sent["user_agent"] == "AIMScribe/3.8.3"
    assert sent["signed"] == "yes" and sent["pc_clock"] == "312 s ahead of the server"
    assert sent["device_token"].endswith("a PC we know") and "tok-1234567890" not in str(sent)
    assert sent["fields"]["seq_no"] == 4 and sent["fields"]["session_id"] == "S1"
    assert sent["fields"]["patient_ref"] == "sent (4 chars)"
    assert sent["fields"]["audio"] == {"sample_rate": 44100, "channels": 1}


def test_an_unknown_field_is_kept_as_its_kind_and_size_only():
    request = _request("/api/v2/device/register", {"Content-Type": "application/json"})
    body = json.dumps({"something_new": "a long free text that might hold anything",
                       "enrollment_token": "one-time-secret"}).encode()
    sent = device_log.what_was_sent(request, body, time.time(), known_pc=False)
    assert sent["fields"]["something_new"] == "text (41 chars)"
    assert sent["fields"]["enrollment_token"].startswith("sent (15 chars, fingerprint ")
    assert "one-time-secret" not in json.dumps(sent)
    assert "device_token" not in sent                  # a join sends none
    assert device_log.flat_sent(sent).startswith("content_type: application/json; fields.")
