# Running the whole system by hand

This is the system on one machine, with a real microphone, driven by hand so
you can watch each step. Nothing here touches a real deployment: the
databases, the storage and the recorder's state all live in one bench folder.

Three windows. Leave all three open.

---

## Before you start: stop any AIMScribe already running

Only one agent may run on a machine, and the one already running wins — you
will be talking to it without knowing. Check the tray for the AIMScribe icon
and exit it, or:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'main.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

---

## Window 1 — the system

```
cd C:\Users\USER\Downloads\AIMScribe\AIMScribe_v3
python tools\bench.py
```

Wait for `Watching. Ctrl+C to stop.` It starts PostgreSQL with both databases,
the AIMS LAB server, a local stand-in for the cloud buckets, and the archive
worker; then it prints your **administrator key** and **CMED key**, and
narrates everything that happens from then on.

This window is the one to watch. Leave it visible.

`--reset` starts from empty databases, and clears the bench agent's enrolment
with them. `--keep` is the default: stop with Ctrl+C, start again, everything
is still there.

---

## Window 2 — the recorder

```
start-bench-recorder.bat
```

(written into the project root by window 1). This is the shipping agent: tray
icon, on-screen Stop and Pause, capturing from this machine's microphone. It
keeps its keys, enrolment and spool in the bench folder, so an agent already
installed on this PC is untouched.

It enrols itself on first start. Window 1 will show it.

---

## Window 3 — CMED's page

```
cd C:\Users\USER\Downloads\AIMScribe\AIMScribe_v3\cmed-web
npm install        (first time only)
npm run dev
```

Open <http://localhost:3000>.

---

## The consultation

1. **Enter a patient** on the first page — a patient number, name, age, and the
   doctor's identifier — and continue. This stands in for CMED opening a
   patient record.

2. **Open patient.** The page sends five plain fields to the recorder and,
   from its own server, sends API 2 to the AIMS LAB server. Window 1 says:

   ```
   ...: recording started
   ...: confirmed by CMED's API 2
   ```

   `RECORDING_STARTED` means the server authorised it; `RECORDING_PROVISIONAL`
   means it is recording while permission is still being checked. Both mean
   the microphone is live.

3. **Speak.** Every 20–30 seconds on this bench (30–60 in a clinic) a piece is
   sealed, uploaded, verified and receipted, and the PC deletes its copy:

   ```
   piece 1 of ...: committed, verified and receipted - the PC may delete its copy
   ```

4. **Prescription built.** The consultation may now end — until this is sent,
   opening the next patient is refused with `GATE_NOT_ARMED`.

5. **Stop**, on the AIMScribe window or the page.

6. **Watch it land.** Within about ten seconds:

   ```
   ...: archived at UIU (5 receipts)
   ARCHIVED  BENCH001_DRBENCH_HOSP003_234714_234754_20260921.wav  (3.4 MB)
             and its clinical JSON beside it
   ```

   and a few seconds later:

   ```
   ...: lossless cloud copy verified; pieces deleted
   ```

---

## Looking at what it produced

Everything is under `%LOCALAPPDATA%\aimscribe-bench`.

| Where | What |
|---|---|
| `archive\<clinic>\<doctor>\<date>\<name>\` | the **WAV** — play it, you will hear the room — with its **JSON**, its manifest, and the day's index |
| `storage\segments\` | pieces waiting to be merged. Empty once a recording is copied |
| `storage\copies\` | the encrypted lossless copy (`.flac.enc`). Unreadable without the key |
| `storage\clinical-json\` | the JSON copy, kept warm |
| `recorder\spool\` | what the PC still holds. Empty is correct: audio goes as soon as it is receipted |

Open the JSON: patient, paramedic readings, previous visit, the prescription
CMED sent, and the recording's fingerprint.

Prove the copy is real (the key is in `bench.json`):

```
set AIMS_COPY_KEY=<copy_key from %LOCALAPPDATA%\aimscribe-bench\bench.json>
python backend\archive_worker\restore.py restore ^
    %LOCALAPPDATA%\aimscribe-bench\storage\copies\<clinic>\<doctor>\<date> ^
    %TEMP%\restored
```

It decrypts each copy and decodes it back to WAV, refusing anything that does
not decode cleanly. Play the result: it is the same consultation.

---

## The dashboard

<http://127.0.0.1:6000/api/v2/dashboard> — it asks for the administrator key
from window 1, and keeps it for the tab only.

Volume by clinic and doctor, confirmation, quarantines and early stops,
every recorder and what it is holding, what CMED never described, and what is
not yet safe in two places. Click any number to see the recordings behind it.

---

## Things worth trying

**The patient says no.** Press Stop on the AIMScribe window and choose
*Patient did not consent*. It asks once, then deletes the recording on the PC
and tells the server, which erases it and everything CMED sent for that visit.
Window 1 says `PATIENT DID NOT CONSENT - erased everywhere`, and the dashboard
counts it under refusals, not under the day's work.

**Open the next patient too early.** Before sending *Prescription built*, try
to open another patient: `409 GATE_NOT_ARMED`. That is what stops a
consultation being cut off mid-sentence.

**Pull the network.** Stop window 1 (Ctrl+C) while recording. The recorder
keeps recording and holds its pieces; the page shows that it is not confirmed.
Start window 1 again and watch the backlog drain.

**Send a broken message.** With nothing else running:

```
python tools\channel_b_test.py --sample patient_information > api2.json
       # edit it: remove demographics
python tools\channel_b_test.py --validate api2.json --kind patient_information
```

It applies the server's own rules and names the field that is wrong.

---

## If something looks wrong

| What you see | What it means |
|---|---|
| `DEVICE_NOT_ENROLLED` | the database was reset but the agent kept its identity. Stop the agent, `python tools\bench.py --reset`, start it again |
| `AGENT_NOT_READY` | the recorder is not running, or another agent holds the port |
| Window 2 exits at once | another AIMScribe is already running |
| `PostgreSQL did not start within 120s` | the bench was killed rather than stopped. `--reset`, or delete the `db` folder |
| Nothing in window 1 while you record | you are talking to a different agent — check window 2 is the one that printed `Agent ready` |
