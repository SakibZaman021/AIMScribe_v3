#!/usr/bin/env python3
"""
Channel A, tried out against a running recorder - the browser half of the CMED
integration, exercised the way CMED's page will exercise it.

    python tools/channel_a_test.py
    python tools/channel_a_test.py --origin https://ehr.example.com --json

Run it on a PC where AIMScribe.exe is running and enrolled. It refuses to start
if anything is already being recorded, so it cannot interfere with a real
consultation.

It records for a few seconds under an obviously fake patient id (TEST_CHA_...).
No API 2 is ever sent for that patient, so the recording is never confirmed and
the server deletes it after 24 hours on its own - nothing to clean up by hand.

What it proves, beyond "the messages parse":
  * a wrong page address is refused before anything else happens
  * a repeated start, or a start for another patient, before the prescription
    is accepted the way any start is (RECORDING_STARTED / _PROVISIONAL, with
    "continued": true and the running session_id) - the recording carries on,
    and CMED's page goes on to send that patient's API 2 as usual
  * after the prescription, the same patient again is SESSION_ALREADY_ACTIVE
  * a dropped socket does not stop a recording - on reconnect the recorder is
    still recording the same session, and says so in its first message
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone, timedelta

try:
    import websockets
except ImportError:
    sys.exit("needs the websockets package: pip install websockets")

URL = "ws://127.0.0.1:5050/ws"
DHAKA = timezone(timedelta(hours=6))


class Run:
    def __init__(self):
        self.rows = []

    def check(self, case, expected, got, detail=""):
        ok = got == expected if not isinstance(expected, tuple) else got in expected
        exp = expected if not isinstance(expected, tuple) else " or ".join(expected)
        self.rows.append({"case": case, "expected": exp, "got": got, "ok": ok,
                          "detail": detail})
        return ok


async def send(ws, msg, rid):
    """Send one command and return the reply carrying our request_id."""
    msg = dict(msg, request_id=rid)
    await ws.send(json.dumps(msg))
    while True:
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if reply.get("request_id") == rid:
            return reply
        # unprompted status events arrive in between; skip them


async def first_status(ws):
    """The recorder pushes a status message as soon as the socket opens."""
    for _ in range(10):
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if m.get("event") == "status":
            return m
    return {}


async def main(origin, as_json):
    run = Run()
    now = datetime.now(DHAKA).replace(microsecond=0)
    stamp = now.strftime("%H%M%S")
    visit = {"patient_id": f"TEST_CHA_{stamp}", "doctor_id": "DR0042",
             "hospital_id": "", "start_time": now.isoformat(),
             "date": now.date().isoformat()}

    # 1. a page that is not on the allow-list must not get in at all
    try:
        async with websockets.connect(URL, origin="https://not-registered.example"):
            run.check("A page address that is not registered", "refused", "accepted")
    except Exception:
        run.check("A page address that is not registered", "refused", "refused")

    try:
        ws = await websockets.connect(URL, origin=origin)
    except Exception as exc:
        run.check("Connect from the registered page address", "connected",
                  f"failed: {exc}")
        return report(run, as_json)
    run.check("Connect from the registered page address", "connected", "connected")

    st = await first_status(ws)
    run.check("A status message arrives unprompted", "status", st.get("event", "none"))
    if st.get("is_recording"):
        await ws.close()
        print("Something is already being recorded on this PC. Not touching it - "
              "run this again when the room is free.")
        return 2

    # 2. read the clinic code off the laptop, as the page should
    r = await send(ws, {"command": "doctors"}, "t-doctors")
    clinic = (r.get("data") or {}).get("hospital_id")
    run.check("doctors returns this PC's clinic", "OK", r.get("code"),
              f"hospital_id={clinic}")
    visit["hospital_id"] = clinic or "AALO_DHOLPUR"

    # 3. malformed starts are refused before the microphone opens
    bad = dict(visit)
    bad.pop("start_time")
    r = await send(ws, {"command": "start", "trigger": bad}, "t-missing")
    run.check("API 1 with no start_time", "MISSING_FIELD", r.get("code"))

    bad = dict(visit, patient_id="TEST CHA")
    r = await send(ws, {"command": "start", "trigger": bad}, "t-badid")
    run.check("API 1 with a space in patient_id", "INVALID_IDENTIFIER", r.get("code"))

    r = await send(ws, {"command": "prescription_built", "patient_id": visit["patient_id"],
                        "session_id": "01AAAAAAAAAAAAAAAAAAAAAAAA"}, "t-noactive")
    run.check("API 3a with nothing recording", "NO_ACTIVE_SESSION", r.get("code"))

    # 4. the real start
    r = await send(ws, {"command": "start", "trigger": visit}, "t-start")
    session_id = (r.get("data") or {}).get("session_id")
    run.check("API 1, a good start", ("RECORDING_STARTED", "RECORDING_PROVISIONAL"),
              r.get("code"), f"session_id={session_id}")
    if not session_id:
        await ws.close()
        return report(run, as_json)

    # 5. THE RECOVERY PROPERTY: resending the same start is harmless
    def continued(reply):
        d = reply.get("data") or {}
        ok = (reply.get("code") in ("RECORDING_STARTED", "RECORDING_PROVISIONAL")
              and d.get("continued") is True and d.get("session_id") == session_id)
        return "accepted, same recording" if ok else f"{reply.get('code')} {d}"

    r = await send(ws, {"command": "start", "trigger": visit}, "t-again")
    run.check("API 1 again, same five fields (a safe retry)", "accepted, same recording",
              continued(r))

    other = dict(visit, patient_id=f"TEST_CHB_{stamp}")
    r = await send(ws, {"command": "start", "trigger": other}, "t-gate")
    # Accepted, not refused: the recording carries on and a signed marker notes
    # the click; the server decides later whose consultation each part was.
    run.check("API 1 for another patient before the prescription",
              "accepted, same recording", continued(r))
    r = await send(ws, {"command": "start", "trigger": visit}, "t-back")
    run.check("API 1 back to the first patient", "accepted, same recording", continued(r))

    await asyncio.sleep(3)

    # 6. THE OTHER RECOVERY PROPERTY: a dropped socket does not stop recording
    await ws.close()
    await asyncio.sleep(1)
    ws = await websockets.connect(URL, origin=origin)
    st = await first_status(ws)
    same = st.get("session_id") == session_id and bool(st.get("is_recording"))
    run.check("After the socket drops and reconnects", "still recording",
              "still recording" if same else "stopped",
              f"session={st.get('session_id')} level={st.get('level')}")

    # 7. the gate
    r = await send(ws, {"command": "prescription_built", "patient_id": "SOMEONE_ELSE",
                        "session_id": session_id}, "t-mismatch")
    run.check("API 3a naming the wrong patient", "PATIENT_MISMATCH", r.get("code"))

    r = await send(ws, {"command": "prescription_built", "patient_id": visit["patient_id"],
                        "session_id": session_id}, "t-arm")
    run.check("API 3a, the prescription is built", "GATE_ARMED", r.get("code"))

    r = await send(ws, {"command": "prescription_built", "patient_id": visit["patient_id"],
                        "session_id": session_id}, "t-arm2")
    run.check("API 3a again", "GATE_ALREADY_ARMED", r.get("code"))

    r = await send(ws, {"command": "start", "trigger": visit}, "t-armed-again")
    run.check("API 1 for the same patient after the prescription", "SESSION_ALREADY_ACTIVE",
              r.get("code"))

    r = await send(ws, {"command": "status"}, "t-status")
    d = r.get("data") or {}
    run.check("Still recording after API 3a (the gate stops nothing)", "recording",
              "recording" if d.get("is_recording") else "stopped",
              f"armed={d.get('armed')}")

    # 8. finish. A page never sends stop; this test does, as the doctor would.
    r = await send(ws, {"command": "stop", "reason": "doctor_stopped"}, "t-stop")
    run.check("Stop (the doctor's control, for the test only)", "OK", r.get("code"))
    await ws.close()
    await asyncio.sleep(1)

    # ------------------------------------------------------------------ recovery
    # The two failures section 11A of the integration document says a page can
    # heal on its own, reproduced and healed here exactly as reconcile() would.

    # R1. The start never arrived: the doctor opened the patient while the socket
    #     was down. The page reconnects, sees the recorder idle, and resends the
    #     SAME five fields - including a start_time that is now in the past.
    lost = dict(visit, patient_id=f"TEST_CHR_{stamp}",
                start_time=(now - timedelta(seconds=40)).isoformat())
    ws = await websockets.connect(URL, origin=origin)
    st = await first_status(ws)
    idle = not st.get("is_recording")
    run.check("Recovery: reconnect finds the recorder idle", "idle",
              "idle" if idle else "recording")
    r = await send(ws, {"command": "start", "trigger": lost}, "t-r1")
    rec_id = (r.get("data") or {}).get("session_id")
    run.check("Recovery: late start with the ORIGINAL start_time",
              ("RECORDING_STARTED", "RECORDING_PROVISIONAL"), r.get("code"),
              "start_time 40 s in the past")
    r = await send(ws, {"command": "status"}, "t-r1s")
    d = r.get("data") or {}
    run.check("Recovery: the right patient is now recording", lost["patient_id"],
              d.get("patient_ref"))

    # R2. API 3a was lost: the page built the prescription, but the recorder still
    #     says armed=false. The page resends it.
    r = await send(ws, {"command": "status"}, "t-r2s")
    unarmed = (r.get("data") or {}).get("armed") is False
    run.check("Recovery: status shows the gate unarmed", "armed=false",
              "armed=false" if unarmed else "armed=true")
    r = await send(ws, {"command": "prescription_built", "patient_id": lost["patient_id"],
                        "session_id": rec_id}, "t-r2")
    run.check("Recovery: resent API 3a arms it", "GATE_ARMED", r.get("code"))

    r = await send(ws, {"command": "stop", "reason": "doctor_stopped"}, "t-r-stop")
    run.check("Recovery: stop (test only)", "OK", r.get("code"))
    await ws.close()
    return report(run, as_json)


def report(run, as_json):
    passed = sum(r["ok"] for r in run.rows)
    if as_json:
        print(json.dumps({"passed": passed, "total": len(run.rows), "rows": run.rows},
                         indent=2))
    else:
        print(f"Trying Channel A against {URL}\n")
        print(f"{'case':58} {'expected':26} {'got':26}")
        print("-" * 112)
        for r in run.rows:
            mark = "" if r["ok"] else "   <-- FAILED"
            print(f"{r['case']:58} {r['expected']:26} {str(r['got']):26}{mark}")
        print(f"\n{passed}/{len(run.rows)} as expected")
        print("Channel A: READY" if passed == len(run.rows) else "Channel A: NOT READY")
    return 0 if passed == len(run.rows) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--origin", default="http://localhost:3000",
                    help="the page address registered on the recorder's allow-list")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.origin, a.json)))
