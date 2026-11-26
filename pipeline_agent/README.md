# AIMScribe pipeline agent

Runs on the lab GPU machine (role `lab`) and on the quality service (role `metrics`).
It does all the security: signed requests, the job ticket, opening the sealed audio,
checking it is the archived recording, keeping the job's lease, and deleting every
working file when a job ends. Your model is one command that reads files and writes files.

Never put an R2 key, a database login or any AIMScribe secret on these machines.
The agent needs none.

## 1. Set up once

```bash
pip install cryptography requests
python agent.py keygen --keys /etc/aimscribe-agent
```

This prints `sign_pubkey` and `box_pubkey`. Send both to the AIMScribe administrator,
together with the machine's fixed public IP address. The private keys stay in
`/etc/aimscribe-agent` (readable by the agent's user only) and never leave the machine.
The administrator enrols the machine and sends back its **machine id**.

## 2. Run

```bash
python agent.py run --server https://aimscribe.iriic.uiu.ac.bd \
    --machine <machine id> --keys /etc/aimscribe-agent \
    --role lab --command "python /opt/rahat/run.py" --parallel 2
```

Run it as a service (systemd) so it restarts with the machine. It only makes outbound
HTTPS calls, so no port has to be opened.

## 3. What your command receives and must write

The agent calls your command with these arguments added at the end:

| Role | Called as | Must write in `--out` |
|---|---|---|
| `lab` | `<command> --audio <in.wav> --out <dir>` | `transcript.json`, `ner.json`, `model.json` |
| `metrics` | `<command> --inputs <in.json> --out <dir>` | `metrics.json`, `model.json` |

Exit with a non-zero code on failure; the agent reports it and the job is retried later.
Any other file you write is deleted with the job.

### model.json (both roles)

```json
{"model": "rahat-bn-asr", "model_version": "2026.11.1", "weights_sha256": "<64 hex, optional>"}
```

### transcript.json

```json
{
  "language": "bn",
  "text": "the whole transcript",
  "segments": [{"start": 0.0, "end": 2.5, "text": "...", "speaker": "doctor"}],
  "english_text": "optional English version"
}
```

`language` is `bn`, `en` or `mixed`. Segment times are seconds from the start of the WAV.
`speaker` is optional. At most 2 MB.

### ner.json

```json
{"entities": [{"type": "drug", "text": "Paracetamol", "start": 120, "end": 131,
               "normalized": "paracetamol", "confidence": 0.93}]}
```

`type` is letters and `_` only, up to 32 characters (for example `drug`, `dose`,
`diagnosis`, `symptom`, `test`, `advice`). `start`/`end` are character positions in
`transcript.json`'s `text`. Only `type` and `text` are required. At most 1 MB.

### Inputs for `metrics` (in.json)

```json
{"job_id": "cmp_...", "rec": "rec_...", "prescription": { ... CMED's prescription ... },
 "ner": { ... the lab's ner.json ... }}
```

No patient identifiers are included.

### metrics.json

```json
{"scores": {"drug_recall": 0.82, "dose_match": 0.95}, "details": {"optional": "anything"}}
```

At most 200 scores and 256 KB in total.

## 4. For the quality dashboard (role `metrics` only)

Signed with the same keys (see `Server.call` in `agent.py`):

- `GET /api/v2/pipeline/metrics?since=<ISO time>` returns every metrics result since then, with
  `rec`, clinic, doctor and date. It never includes patient IDs.
- `GET /api/v2/pipeline/recordings/<rec>/transcript` returns one recording's transcript when
  someone opens it. Each read is logged.
