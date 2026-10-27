"""
Database access for the v2 integrity layer.

Kept separate from `database/postgres_async.py` so the existing transcription and
NER pipeline is untouched; this module only ever reads and writes the tables
created by `scripts/002_v2_integrity.sql`. It shares that module's connection
pool, because Neon's connection budget is small.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

import integrity
from integrity import ChainEntry

logger = logging.getLogger(__name__)

# One lock id for the audit chain. Appending has to be serialised or two
# concurrent writers can both read the same prev_hash and fork the chain.
AUDIT_LOCK_ID = 0x41494D53  # 'AIMS'


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# The connection of the session transaction this task is inside, if any:
# (pool, connection, task). Every query this repository makes from that task
# runs on that connection, so a request's steps commit or roll back together
# and see each other's writes. Bound to the task, not merely the context: a
# task started from inside the transaction copies the context, and must not
# borrow a connection that another task is using.
_SESSION_TX: ContextVar[Optional[tuple]] = ContextVar("aims_session_tx", default=None)


class _Borrowed:
    """The transaction's connection, lent without being released."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def hash_token(token: str) -> bytes:
    return integrity.sha256_bytes(token.encode("utf-8"))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _erase_row(row) -> Dict[str, Any]:
    out = dict(row)
    for key in ("files", "cloud_keys"):
        if isinstance(out.get(key), str):
            out[key] = json.loads(out[key])
    return out


class V2Repository:
    """All v2 queries. Every method takes and releases one pooled connection,
    or - inside session_tx - uses that transaction's."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    def _acquire(self):
        held = _SESSION_TX.get()
        if held is not None and held[0] is self._pool:
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if held[2] is current:
                return _Borrowed(held[1])
        return self._pool.acquire()

    @asynccontextmanager
    async def session_tx(self, session_id: str):
        """
        One recording, one writer at a time.

        Commit, close, refuse, archive and every chain entry take this before
        they read the chain head or the session's state, and do their writes
        inside it: two of them for the same recording can no longer both read
        the same head, or one decide on a state the other is changing. A
        per-session advisory lock, so a session not yet stored (a refusal made
        offline) is locked too; and the row itself FOR NO KEY UPDATE, so
        writers that do not take the lock still wait for this one, while
        inserts into tables that reference the row are not blocked.
        Re-entrant within one task.
        """
        held = _SESSION_TX.get()
        if held is not None and held[0] is self._pool:
            try:
                same_task = held[2] is asyncio.current_task()
            except RuntimeError:
                same_task = False
            if same_task:
                yield held[1]
                return
        async with self._acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                                   "aims-session:" + session_id)
                await conn.execute("SELECT 1 FROM sessions WHERE session_id = $1 "
                                   "FOR NO KEY UPDATE", session_id)
                try:
                    task = asyncio.current_task()
                except RuntimeError:
                    task = None
                token = _SESSION_TX.set((self._pool, conn, task))
                try:
                    yield conn
                finally:
                    _SESSION_TX.reset(token)

    # ============================================================
    # Hospitals
    # ============================================================

    async def upsert_hospital(self, hospital_id: str, name: str, timezone_name: str,
                              only_if_new: bool = False) -> None:
        """
        `only_if_new` is for sites first seen on a trigger: record that they
        exist without overwriting a name and timezone an administrator set.
        """
        if only_if_new:
            async with self._acquire() as conn:
                await conn.execute("""
                    INSERT INTO hospitals (hospital_id, name, timezone)
                    VALUES ($1, $2, $3) ON CONFLICT (hospital_id) DO NOTHING
                """, hospital_id, name, timezone_name)
            return
        async with self._acquire() as conn:
            await conn.execute("""
                INSERT INTO hospitals (hospital_id, name, timezone)
                VALUES ($1, $2, $3)
                ON CONFLICT (hospital_id) DO UPDATE
                   SET name = EXCLUDED.name, timezone = EXCLUDED.timezone
            """, hospital_id, name, timezone_name)

    async def hospital_timezone(self, hospital_id: str) -> str:
        async with self._acquire() as conn:
            tz = await conn.fetchval(
                "SELECT timezone FROM hospitals WHERE hospital_id = $1", hospital_id)
        return tz or "Asia/Dhaka"

    # ============================================================
    # Enrollment
    # ============================================================

    async def create_enrollment_token(
        self, *, hospital_id: str, doctor_id: str, created_by: str,
        ttl_hours: int = 72
    ) -> str:
        """
        Mint a single-use token. Only the hash is stored.

        The token carries both the hospital and the doctor, so the machine
        receives its identity rather than asserting one. This is the only point
        at which a doctor is named, and an administrator does it.
        """
        token = new_token()
        async with self._acquire() as conn:
            await conn.execute("""
                INSERT INTO enrollment_tokens
                    (token_sha256, hospital_id, doctor_id, created_by, expires_at)
                VALUES ($1, $2, $3, $4, $5)
            """, hash_token(token), hospital_id, doctor_id, created_by,
                 _utcnow() + timedelta(hours=ttl_hours))
        return token

    async def enroll_device(
        self,
        *,
        token: str,
        tpm_pubkey: bytes,
        machine_name: str,
        os_version: str,
        app_version: str,
        protocol_version: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Consume an enrollment token and register the device.

        Returns None when the token is unknown, expired, or already used. The
        whole thing runs in one transaction so a token cannot be redeemed twice
        by two concurrent requests.
        """
        device_token = new_token()

        async with self._acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT hospital_id, doctor_id, expires_at, used_at
                    FROM enrollment_tokens
                    WHERE token_sha256 = $1
                    FOR UPDATE
                """, hash_token(token))

                if row is None:
                    return None
                if row["used_at"] is not None:
                    # A token is single-use, but "used" is not the same as
                    # "working". If the agent's write of its identity failed
                    # after the server had already committed - a full disk, a
                    # permissions problem, a crash between the two files - the
                    # machine has no credential and its token is spent. It can
                    # never recover on its own, and the only symptom is the PC
                    # reporting that it is not enrolled while the server shows
                    # it enrolled perfectly well. That happened.
                    #
                    # So a token may be presented again by the same machine, but
                    # only while the device it created has never once been seen.
                    # After the first heartbeat this is a replay and is refused.
                    previous = await conn.fetchrow("""
                        SELECT device_id, tpm_pubkey, last_seen_at
                          FROM devices WHERE device_id = $1
                    """, row["device_id"])
                    if previous is None or previous["last_seen_at"] is not None:
                        logger.warning("Enrollment token replay attempt")
                        return None
                    if bytes(previous["tpm_pubkey"]) != tpm_pubkey:
                        # A different machine, or the same one with a new key.
                        # Either way this is not the retry it claims to be.
                        logger.warning("Enrollment retry presented a different device key")
                        return None

                    logger.warning(
                        "Re-issuing credentials for device %s: it enrolled but was "
                        "never seen, so its first attempt did not complete",
                        previous["device_id"])
                    await conn.execute("""
                        UPDATE devices
                           SET token_sha256 = $2, app_version = $3, machine_name = $4
                         WHERE device_id = $1
                    """, previous["device_id"], hash_token(device_token),
                         app_version, machine_name)
                    return {
                        "device_id": str(previous["device_id"]),
                        "hospital_id": row["hospital_id"],
                        "doctor_id": row["doctor_id"],
                        "device_token": device_token,
                    }
                if row["expires_at"] < _utcnow():
                    logger.warning("Expired enrollment token presented")
                    return None

                device = await conn.fetchrow("""
                    INSERT INTO devices
                        (hospital_id, doctor_id, tpm_pubkey, token_sha256,
                         machine_name, os_version, app_version, protocol_version)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING device_id, hospital_id, doctor_id
                """, row["hospital_id"], row["doctor_id"], tpm_pubkey,
                     hash_token(device_token), machine_name, os_version,
                     app_version, protocol_version)

                await conn.execute("""
                    UPDATE enrollment_tokens
                       SET used_at = now(), device_id = $2
                     WHERE token_sha256 = $1
                """, hash_token(token), device["device_id"])

        return {
            "device_id": str(device["device_id"]),
            "hospital_id": device["hospital_id"],
            # The machine learns which doctor it belongs to here, and nowhere
            # else. Nothing in the browser can change it.
            "doctor_id": device["doctor_id"],
            "device_token": device_token,
        }

    async def device_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        async with self._acquire() as conn:
            row = await conn.fetchrow("""
                SELECT device_id, hospital_id, tpm_pubkey, app_version, revoked_at,
                       signs_requests, approval
                FROM devices WHERE token_sha256 = $1
            """, hash_token(token))
        return dict(row) if row else None

    # ============================================================
    # PCs that register themselves (no enrolment token)
    # ============================================================

    async def register_device(self, *, device_id, tpm_pubkey: bytes, hospital_id: str,
                              machine_name: str, os_version: str, app_version: str,
                              protocol_version: int, registered_ip: Optional[str],
                              pending_per_clinic: int, per_ip_per_day: int
                              ) -> Dict[str, Any]:
        """
        A PC that proved it holds its key, at a clinic CMED named, joins as
        pending. Returns {"device_id", "hospital_id", "device_token",
        "approval"} or {"refused": reason}.

        Asked again (the answer was lost on the way), it gets a new token only
        while the server has never heard from it with the first one: after
        that, a replayed registration could otherwise take its token away.
        """
        token = new_token()
        async with self._acquire() as conn:
            async with conn.transaction():
                # One registration per clinic at a time, so the cap holds.
                await conn.execute("SELECT 1 FROM hospitals WHERE hospital_id = $1 FOR UPDATE",
                                   hospital_id)
                existing = await conn.fetchrow(
                    "SELECT * FROM devices WHERE device_id = $1 FOR UPDATE", device_id)
                if existing is not None:
                    if bytes(existing["tpm_pubkey"]) != tpm_pubkey:
                        return {"refused": "key_mismatch"}
                    if existing["revoked_at"] is not None or existing["approval"] == "rejected":
                        return {"refused": "revoked"}
                    if existing["hospital_id"] != hospital_id:
                        return {"refused": "other_clinic"}
                    if existing["last_seen_at"] is not None:
                        return {"refused": "already_registered"}
                    await conn.execute("UPDATE devices SET token_sha256 = $2 WHERE device_id = $1",
                                       device_id, hash_token(token))
                    return {"device_id": str(device_id), "hospital_id": hospital_id,
                            "device_token": token, "approval": existing["approval"]}

                pending = await conn.fetchval("""
                    SELECT count(*) FROM devices
                     WHERE hospital_id = $1 AND approval = 'pending' AND revoked_at IS NULL
                """, hospital_id)
                if pending >= pending_per_clinic:
                    return {"refused": "too_many_pending"}
                if registered_ip:
                    recent = await conn.fetchval("""
                        SELECT count(*) FROM devices
                         WHERE registered_via = 'self' AND registered_ip = $1
                           AND enrolled_at > now() - interval '1 day'
                    """, registered_ip)
                    if recent >= per_ip_per_day:
                        return {"refused": "too_many_from_address"}
                await conn.execute("""
                    INSERT INTO devices (device_id, hospital_id, tpm_pubkey, token_sha256,
                                         machine_name, os_version, app_version,
                                         protocol_version, signs_requests, approval,
                                         registered_via, registered_ip)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8, true, 'pending', 'self', $9)
                """, device_id, hospital_id, tpm_pubkey, hash_token(token),
                     machine_name[:128], os_version[:256], app_version[:32],
                     protocol_version, registered_ip)
        return {"device_id": str(device_id), "hospital_id": hospital_id,
                "device_token": token, "approval": "pending"}

    async def device_upload_bytes(self, device_id) -> int:
        """Everything this PC has uploaded and the server still holds."""
        async with self._acquire() as conn:
            return int(await conn.fetchval("""
                SELECT COALESCE(sum(g.bytes), 0) FROM segments g
                  JOIN sessions s ON s.session_id = g.session_id
                 WHERE s.device_id = $1
            """, device_id))

    async def set_device_approval(self, device_id, approval: str, *, by: str) -> bool:
        """An administrator's decision on a pending PC. Rejecting also revokes."""
        async with self._acquire() as conn:
            if approval == "approved":
                result = await conn.execute("""
                    UPDATE devices SET approval = 'approved', approved_at = now(),
                                       approved_by = $2
                     WHERE device_id = $1 AND approval = 'pending' AND revoked_at IS NULL
                """, device_id, by[:128])
            else:
                result = await conn.execute("""
                    UPDATE devices SET approval = 'rejected', revoked_at = now(),
                                       revoked_reason = $2, token_sha256 = NULL
                     WHERE device_id = $1 AND approval = 'pending'
                """, device_id, f"rejected by {by}"[:500])
        return result.endswith("1")

    async def expire_pending_devices(self, days: int) -> List[Dict[str, Any]]:
        """Pending this long without CMED confirming two of its recordings: rejected."""
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                UPDATE devices
                   SET approval = 'rejected', revoked_at = now(), token_sha256 = NULL,
                       revoked_reason = 'not confirmed by CMED within '
                                        || $1::text || ' days of registering'
                 WHERE approval = 'pending' AND revoked_at IS NULL
                   AND enrolled_at < now() - make_interval(days => $1)
             RETURNING device_id, hospital_id, machine_name
            """, days)
        return [dict(r) for r in rows]

    async def approvals_to_audit(self) -> List[Dict[str, Any]]:
        """PCs the database approved (two confirmed recordings) not yet in the audit log."""
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                UPDATE devices SET approval_audited = true
                 WHERE NOT approval_audited
             RETURNING device_id, hospital_id, machine_name, approved_by
            """)
        return [dict(r) for r in rows]

    async def touch_device(
        self, device_id, *, spool_bytes: Optional[int] = None,
        pending_segments: Optional[int] = None, app_version: Optional[str] = None,
        held_sessions: Optional[List[Dict[str, Any]]] = None,
        held_count: Optional[int] = None, disk_encrypted: Optional[bool] = None,
        clock_offset_seconds: Optional[float] = None,
        microphone: Optional[str] = None, microphone_is_clinic: Optional[bool] = None,
    ) -> None:
        """
        One heartbeat. Fields an older recorder does not send are left as
        they were; held_sessions is replaced whole, so a list that empties
        clears the dashboard.
        """
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE devices
                   SET last_seen_at = now(),
                       last_heartbeat_at = now(),
                       spool_bytes = COALESCE($2, spool_bytes),
                       pending_segments = COALESCE($3, pending_segments),
                       app_version = COALESCE($4, app_version),
                       held_sessions = COALESCE($5::jsonb, held_sessions),
                       held_count = COALESCE($6, held_count),
                       disk_encrypted = COALESCE($7, disk_encrypted),
                       clock_offset_seconds = COALESCE($8, clock_offset_seconds),
                       microphone = COALESCE($9, microphone),
                       microphone_is_clinic = COALESCE($10, microphone_is_clinic)
                 WHERE device_id = $1
            """, device_id, spool_bytes, pending_segments, app_version,
                 json.dumps(held_sessions) if held_sessions is not None else None,
                 held_count, disk_encrypted, clock_offset_seconds, microphone,
                 microphone_is_clinic)

    async def note_session_microphone(self, device_id, session_id: str,
                                      microphone: str) -> None:
        """A microphone this PC's open recording used; each name kept once, in order."""
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE sessions
                   SET microphones = array_append(COALESCE(microphones, '{}'), $3)
                 WHERE session_id = $1 AND device_id = $2 AND closed_at IS NULL
                   AND NOT ($3 = ANY(COALESCE(microphones, '{}')))
            """, session_id, device_id, microphone)

    async def use_device_nonce(self, device_id, nonce: str) -> bool:
        """
        True the first time this device sends this nonce. Nonces older than
        the signature window are swept a few hundred at a time, so the table
        stays the size of fifteen minutes of traffic. One statement, so one
        round trip on every signed request.
        """
        async with self._acquire() as conn:
            fresh = await conn.fetchval("""
                WITH swept AS (
                    DELETE FROM device_nonces WHERE (device_id, nonce) IN (
                        SELECT device_id, nonce FROM device_nonces
                         WHERE seen_at < now() - interval '15 minutes' LIMIT 200)
                )
                INSERT INTO device_nonces (device_id, nonce) VALUES ($1, $2)
                ON CONFLICT DO NOTHING RETURNING 1
            """, device_id, nonce)
        return bool(fresh)

    async def mark_device_signs(self, device_id) -> None:
        """From now on this PC's requests must be signed: it has shown it can."""
        async with self._acquire() as conn:
            await conn.execute("UPDATE devices SET signs_requests = true "
                               "WHERE device_id = $1 AND NOT signs_requests", device_id)

    async def quarantine_device_sessions(self, device_id, reason: str) -> int:
        """
        Hold every recording of this PC not yet in the dataset. Used when a PC
        is revoked as untrustworthy rather than merely lost.
        """
        async with self._acquire() as conn:
            result = await conn.execute("""
                UPDATE sessions
                   SET status = 'quarantined', quarantine_reason = $2, updated_at = now()
                 WHERE device_id = $1 AND archived_at IS NULL
                   AND quarantine_reason IS NULL
                   AND status NOT IN ('quarantined', 'archived', 'refused', 'expired')
            """, device_id, reason[:500])
        return int(result.split()[-1]) if result else 0

    async def revoke_device(self, device_id, reason: str) -> bool:
        async with self._acquire() as conn:
            result = await conn.execute("""
                UPDATE devices SET revoked_at = now(), revoked_reason = $2,
                                   token_sha256 = NULL
                 WHERE device_id = $1 AND revoked_at IS NULL
            """, device_id, reason)
        return result.endswith("1")

    # ============================================================
    # Sessions
    # ============================================================

    async def open_session(
        self,
        *,
        session_id: str,
        hospital_id: str,
        doctor_id: str,
        patient_id: str,
        device_id,
        session_date,
        opened_at: datetime,
        audio: Dict[str, int],
        consent_method: str,
        genesis: ChainEntry,
        object_prefix: Optional[str] = None,
    ) -> bool:
        """
        Create a v2 session and store its genesis chain entry atomically.

        Idempotent: an agent that retries after a network failure gets True again
        rather than a duplicate-key error.
        """
        async with self._acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchval(
                    "SELECT session_id FROM sessions WHERE session_id = $1", session_id)
                if existing:
                    return True

                await conn.execute("""
                    INSERT INTO sessions
                        (session_id, patient_id, doctor_id, hospital_id, device_id,
                         protocol_version, status, session_date, opened_at,
                         recording_date, sample_rate, channels, sample_width,
                         consent_obtained, consent_method, consent_at,
                         object_prefix)
                    VALUES ($1,$2,$3,$4,$5, 2, 'active', $6,$7,$6, $8,$9,$10,
                            TRUE, $11, $7, $12)
                """, session_id, patient_id, doctor_id, hospital_id, device_id,
                     session_date, opened_at,
                     audio.get("sample_rate"), audio.get("channels"),
                     audio.get("sample_width"), consent_method, object_prefix)

                await self._insert_chain_entry(conn, session_id, genesis)
        return True

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        async with self._acquire() as conn:
            row = await conn.fetchrow("""
                SELECT session_id, patient_id, doctor_id, hospital_id, device_id,
                       protocol_version, status, session_date, opened_at, closed_at,
                       segment_count, chain_head_hash, archive_relpath, archived_at,
                       archive_sha256, archive_bytes, total_duration_seconds,
                       quarantine_reason, sample_rate, channels, sample_width,
                       object_prefix, confirmation, grant_jti, file_stem,
                       local_start, local_end, copied_at, segments_deleted_at,
                       json_stale, parts_count, parts_stale, continues_session_id
                FROM sessions WHERE session_id = $1
            """, session_id)
        return dict(row) if row else None

    async def close_session(
        self, session_id: str, *, closed_at: datetime, duration_seconds: float,
        paused_seconds: float, segment_count: int, chain_head: bytes,
        manifest: Dict[str, Any], close_reason: str = "",
        local_date=None, local_start=None, local_end=None,
    ) -> None:
        """
        Close the session and record its wall-clock columns.

        The wall clock is worked out by the caller, in Python, and passed in.
        It used to be `AT TIME ZONE h.timezone` inside this statement, which
        made every close depend on the database knowing the clinic's zone by
        name: a PostgreSQL build without the timezone database, or a clinic
        registered with a zone that does not exist, turned every close into a
        500 that the recorder retried for ever while nothing was archived.
        opened_at and closed_at stay UTC - one set of columns is for reading,
        the other for arithmetic, and conflating them is how evening
        consultations end up filed under the previous day.
        """
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE sessions
                   SET closed_at = $2, total_duration_seconds = $3,
                       paused_seconds = $4, segment_count = $5,
                       chain_head_hash = $6, chain_verified_at = now(),
                       manifest = $7, close_reason = $8,
                       status = 'closed', updated_at = now(),
                       recording_date = COALESCE($9::date, recording_date),
                       start_time     = COALESCE($10::time, start_time),
                       end_time       = COALESCE($11::time, end_time)
                 WHERE session_id = $1
                   -- A quarantined, archived, refused or erased recording is
                   -- never turned back into a closed one by a late or
                   -- repeated close.
                   AND status NOT IN ('quarantined', 'archived', 'refused', 'expired')
                   AND quarantine_reason IS NULL
            """, session_id, closed_at, duration_seconds, paused_seconds,
                 segment_count, chain_head, json.dumps(manifest, ensure_ascii=False),
                 close_reason or None, local_date, local_start, local_end)

    async def quarantine_session(self, session_id: str, reason: str) -> None:
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE sessions
                   SET status = 'quarantined', quarantine_reason = $2, updated_at = now()
                 WHERE session_id = $1
            """, session_id, reason[:500])

    # ============================================================
    # Chain
    # ============================================================

    @staticmethod
    async def _insert_chain_entry(conn, session_id: str, entry: ChainEntry) -> str:
        """
        'stored', 'duplicate' (this exact entry is already there) or 'conflict'
        (another entry holds its number). It used to be ON CONFLICT DO NOTHING,
        which threw a rival entry away in silence while the segment row that
        pointed at it was kept: audio archived without ever being chained.
        """
        try:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO chain_entries
                        (session_id, entry_no, entry_type, payload, payload_sha256,
                         prev_hash, entry_hash, device_signature, occurred_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """, session_id, entry.entry_no, entry.entry_type,
                     json.dumps(entry.payload, ensure_ascii=False),
                     entry.payload_sha256, entry.prev_hash, entry.entry_hash,
                     entry.signature, entry.occurred_at)
        except asyncpg.UniqueViolationError:
            stored = await conn.fetchval("""
                SELECT entry_hash FROM chain_entries
                 WHERE session_id = $1 AND entry_no = $2
            """, session_id, entry.entry_no)
            if stored is not None and bytes(stored) == entry.entry_hash:
                return "duplicate"
            return "conflict"
        return "stored"

    async def append_chain_entry(self, session_id: str, entry: ChainEntry) -> str:
        async with self._acquire() as conn:
            return await self._insert_chain_entry(conn, session_id, entry)

    async def chain_tip(self, session_id: str) -> Optional[Tuple[int, bytes]]:
        """(entry_no, entry_hash) of the newest entry, or None for an empty chain."""
        async with self._acquire() as conn:
            row = await conn.fetchrow("""
                SELECT entry_no, entry_hash FROM chain_entries
                 WHERE session_id = $1 ORDER BY entry_no DESC LIMIT 1
            """, session_id)
        return (int(row["entry_no"]), bytes(row["entry_hash"])) if row else None

    async def genesis_in_use(self, entry_hash: bytes, session_id: str) -> bool:
        """Another recording already opened with this exact genesis entry."""
        async with self._acquire() as conn:
            return bool(await conn.fetchval("""
                SELECT 1 FROM chain_entries
                 WHERE entry_no = 0 AND entry_hash = $1 AND session_id <> $2
            """, entry_hash, session_id))

    async def entry_already_stored(self, session_id: str, entry: ChainEntry) -> bool:
        """
        Is this exact entry already in the chain?

        A commit that succeeded but whose response was lost gets retried by the
        agent. The entry arriving the second time is byte-identical, but the
        stored head has moved past it, so verifying prev_hash judges it a
        violation and quarantines a session that is perfectly intact. Comparing
        the hash tells a duplicate from a forgery: only the original entry
        hashes to the original value.
        """
        async with self._acquire() as conn:
            stored = await conn.fetchval("""
                SELECT entry_hash FROM chain_entries
                 WHERE session_id = $1 AND entry_no = $2
            """, session_id, entry.entry_no)
        return stored is not None and bytes(stored) == entry.entry_hash

    async def chain_head(self, session_id: str) -> Optional[bytes]:
        """Hash of the highest-numbered entry, or None for an empty chain."""
        async with self._acquire() as conn:
            return await conn.fetchval("""
                SELECT entry_hash FROM chain_entries
                 WHERE session_id = $1
                 ORDER BY entry_no DESC LIMIT 1
            """, session_id)

    async def next_entry_no(self, session_id: str) -> int:
        async with self._acquire() as conn:
            highest = await conn.fetchval("""
                SELECT max(entry_no) FROM chain_entries WHERE session_id = $1
            """, session_id)
        return 0 if highest is None else highest + 1

    async def load_chain(self, session_id: str) -> List[ChainEntry]:
        """Ordered by entry_no; verify_chain requires entries in sequence."""
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT entry_no, entry_type, payload, payload_sha256,
                       prev_hash, entry_hash, device_signature
                FROM chain_entries WHERE session_id = $1 ORDER BY entry_no
            """, session_id)

        return [
            ChainEntry(
                entry_no=row["entry_no"],
                entry_type=row["entry_type"],
                payload=json.loads(row["payload"]) if isinstance(row["payload"], str)
                        else row["payload"],
                payload_sha256=bytes(row["payload_sha256"]),
                prev_hash=bytes(row["prev_hash"]) if row["prev_hash"] else None,
                entry_hash=bytes(row["entry_hash"]),
                signature=bytes(row["device_signature"]) if row["device_signature"] else None,
            )
            for row in rows
        ]

    # ============================================================
    # Segments
    # ============================================================

    async def commit_segment(
        self,
        *,
        session_id: str,
        seq_no: int,
        entry: ChainEntry,
        object_key: str,
        byte_length: int,
        duration_seconds: float,
        sha256: bytes,
        rms_mean: Optional[float],
        captured_start_at: Optional[datetime],
        captured_end_at: Optional[datetime],
        is_final: bool,
        clip_name: Optional[str] = None,
    ) -> str:
        """
        Store a verified segment and its chain entry.

        Returns 'stored', or 'duplicate' when the same seq_no arrives again with
        an identical hash - a retry, which must succeed rather than error.
        """
        async with self._acquire() as conn:
            try:
                async with conn.transaction():
                    existing = await conn.fetchrow(
                        "SELECT sha256 FROM segments "
                        " WHERE session_id = $1 AND seq_no = $2", session_id, seq_no)

                    if existing is not None:
                        return ("duplicate" if bytes(existing["sha256"]) == sha256
                                else "conflict")

                    chained = await self._insert_chain_entry(conn, session_id, entry)
                    if chained == "conflict":
                        return "conflict"
                    await conn.execute("""
                        INSERT INTO segments
                            (session_id, seq_no, entry_no, object_key, bytes,
                             duration_seconds, sha256, rms_mean, captured_start_at,
                             captured_end_at, is_final, state, clip_name)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'committed',$12)
                    """, session_id, seq_no, entry.entry_no, object_key, byte_length,
                         duration_seconds, sha256, rms_mean, captured_start_at,
                         captured_end_at, is_final, clip_name)

                    await conn.execute("""
                        UPDATE sessions
                           SET segment_count = (SELECT count(*) FROM segments
                                                 WHERE session_id = $1),
                               updated_at = now()
                         WHERE session_id = $1
                    """, session_id)
            except asyncpg.UniqueViolationError:
                # Two retries of the same piece, in flight at once: the check
                # above found nothing for either, and one of them lost the
                # insert. That is still a duplicate, not a failure - the
                # recorder must be told so rather than being handed a 500 and
                # retrying for ever. Found by the 14-room simulation.
                stored = await conn.fetchrow(
                    "SELECT sha256 FROM segments WHERE session_id = $1 AND seq_no = $2",
                    session_id, seq_no)
                if stored is None:
                    raise
                return "duplicate" if bytes(stored["sha256"]) == sha256 else "conflict"
        return "stored"

    async def segments_for(self, session_id: str) -> List[Dict[str, Any]]:
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT seq_no, object_key, clip_name, bytes, duration_seconds,
                       sha256, captured_start_at, captured_end_at, is_final, state,
                       object_deleted_at
                FROM segments WHERE session_id = $1 ORDER BY seq_no
            """, session_id)
        return [dict(row) for row in rows]

    async def doctor_is_credentialed(self, doctor_id: str, hospital_id: str) -> bool:
        """
        May this doctor record at this hospital?

        Checked on every session open. The doctor now comes from CMED so that a
        consulting-room PC can be shared - which means the browser names them
        again, and the browser is not trusted. The register is what makes a
        rotating doctor safe and DR_TEST_001 impossible.
        """
        async with self._acquire() as conn:
            return bool(await conn.fetchval("""
                SELECT 1 FROM doctors
                 WHERE doctor_id = $1 AND hospital_id = $2 AND active
            """, doctor_id, hospital_id))

    async def doctors_at(self, hospital_id: str) -> List[Dict[str, Any]]:
        """The register for one hospital, for the CMED selector."""
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT doctor_id, full_name FROM doctors
                 WHERE hospital_id = $1 AND active
                 ORDER BY full_name
            """, hospital_id)
        return [dict(r) for r in rows]

    async def upsert_doctor(self, *, doctor_id: str, hospital_id: str,
                            full_name: str, active: bool = True,
                            only_if_new: bool = False) -> None:
        """
        Add or update a doctor in the directory.

        `only_if_new` is for the session-open path, which records whoever CMED
        named. It must not overwrite a real name with an id: an administrator
        who entered "Dr Kamrul Hasan" should not have it flattened back to
        "DR003" the next time that doctor records.
        """
        async with self._acquire() as conn:
            if only_if_new:
                await conn.execute("""
                    INSERT INTO doctors (doctor_id, hospital_id, full_name, active)
                    VALUES ($1, $2, $3, TRUE)
                    ON CONFLICT (doctor_id, hospital_id) DO NOTHING
                """, doctor_id, hospital_id, full_name)
                return
            await conn.execute("""
                INSERT INTO doctors (doctor_id, hospital_id, full_name, active)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (doctor_id, hospital_id) DO UPDATE
                   SET full_name = excluded.full_name,
                       active    = excluded.active,
                       updated_at = now()
            """, doctor_id, hospital_id, full_name, active)

    async def pauses_for(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Every pause and resume, read from the signed chain.

        Taken from chain_entries rather than a summary column so the reason handed
        to the archive is the one the device signed at the time.
        """
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT entry_no, entry_type, occurred_at, payload
                FROM chain_entries
                WHERE session_id = $1 AND entry_type IN ('pause', 'resume')
                ORDER BY entry_no
            """, session_id)

        pauses = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            payload = payload or {}
            pauses.append({
                "entry_no": row["entry_no"],
                "entry_type": row["entry_type"],
                "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
                "reason": payload.get("reason"),
                "authorised_by": payload.get("authorised_by"),
                "seconds": payload.get("seconds"),
            })
        return pauses

    async def mark_object_deleted(self, session_id: str, seq_no: int) -> None:
        """Record that the bucket copy is gone. The archive copy is unaffected."""
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE segments SET object_deleted_at = now()
                 WHERE session_id = $1 AND seq_no = $2
            """, session_id, seq_no)

    async def mark_segments_archived(self, session_id: str,
                                     seq_nos: Optional[List[int]] = None) -> int:
        """The pieces the worker joined, and only those, are now in the archive."""
        async with self._acquire() as conn:
            result = await conn.execute("""
                UPDATE segments SET state = 'archived', archived_at = now()
                 WHERE session_id = $1 AND state = 'committed'
                   AND ($2::int[] IS NULL OR seq_no = ANY($2::int[]))
            """, session_id, seq_nos)
        return int(result.split()[-1]) if result else 0

    # ============================================================
    # Archive (Option B: the AIMS LAB worker pulls)
    # ============================================================

    async def pending_archive(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Sessions that are closed, verified, and not yet archived.

        Selected on the facts - closed, unarchived, unquarantined, with audio -
        rather than on status = 'closed'. The transcription pipeline also writes
        that column and moves a session to 'completed', which quietly removed it
        from this list: its audio was never archived, so no purge receipt was
        ever issued, so it sat on the doctor's PC forever. Nothing was lost, but
        nothing progressed either, and the only visible symptom was disk use.

        Quarantined sessions are excluded: they are held for review, never
        archived automatically and never purged from the agent.

        Sessions with no committed segments are excluded too. There is nothing to
        fetch, so they can never succeed, and leaving them here meant the worker
        retried them on every pass forever.
        """
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT s.session_id, s.hospital_id, s.doctor_id, s.patient_id,
                       s.session_date, s.opened_at, s.closed_at,
                       s.total_duration_seconds, s.segment_count,
                       s.sample_rate, s.channels, s.sample_width, s.manifest,
                       -- The name the server gave this recording when it closed
                       -- (SRS-SES-05). The worker uses it as it stands, so the
                       -- audio, the JSON and the clinical database agree.
                       s.file_stem, s.local_start, s.local_end, s.confirmation,
                       s.parts_count, s.continues_session_id
                FROM sessions s
                WHERE s.closed_at IS NOT NULL
                  AND s.archived_at IS NULL
                  AND s.quarantine_reason IS NULL
                  AND s.status <> 'quarantined'
                  AND s.protocol_version >= 2
                  -- Only a recording CMED confirmed is joined into a WAV
                  -- (confirmation.ARCHIVABLE). One still waiting stays as its
                  -- pieces in the segment bucket - in the cloud, not on this
                  -- server's disk - until it is confirmed or erased.
                  AND s.confirmation = 'confirmed'
                  AND EXISTS (SELECT 1 FROM segments g
                               WHERE g.session_id = s.session_id
                                 AND g.state = 'committed')
                ORDER BY s.closed_at
                LIMIT $1
            """, limit)
        return [dict(row) for row in rows]

    async def mark_archived(
        self, session_id: str, *, relpath: str, sha256: bytes, byte_length: int
    ) -> bool:
        """False when the recording was refused, erased or quarantined meanwhile."""
        async with self._acquire() as conn:
            result = await conn.execute("""
                UPDATE sessions
                   SET archive_relpath = $2, archive_sha256 = $3,
                       archive_bytes = $4, archived_at = now(),
                       status = 'archived', updated_at = now()
                 WHERE session_id = $1
                   AND confirmation NOT IN ('refused', 'expired')
                   AND quarantine_reason IS NULL
                   AND status NOT IN ('quarantined', 'refused', 'expired')
            """, session_id, relpath, sha256, byte_length)
        return bool(result) and result.split()[-1] != "0"

    # ============================================================
    # The cloud copy (SRS-ARC-08..13)
    # ============================================================

    async def pending_copy(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Archived recordings whose lossless cloud copy has not been made.

        Until that copy exists and is verified, the pieces stay in the segment
        bucket: they are the only other copy of the audio (SRS-ARC-08). Only a
        recording CMED has confirmed is copied: the archive bucket holds the
        dataset, and nothing it holds is ever erased for want of CMED.
        """
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT session_id, hospital_id, doctor_id, patient_id, session_date,
                       file_stem, archive_relpath, archive_sha256, archive_bytes,
                       grant_jti, confirmation, parts_count
                  FROM sessions
                 WHERE archived_at IS NOT NULL
                   AND copied_at IS NULL
                   AND NOT parts_stale
                   AND quarantine_reason IS NULL
                   AND archive_relpath IS NOT NULL
                   AND confirmation = 'confirmed'
                 ORDER BY archived_at
                 LIMIT $1
            """, limit)
        return [dict(row) for row in rows]

    async def pending_json(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Archived recordings whose JSON has to be written again, because CMED
        sent the prescription after the recording was archived (SRS-ARC-13).
        """
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT session_id, hospital_id, doctor_id, session_date, file_stem,
                       archive_relpath, grant_jti, parts_count
                  FROM sessions
                 WHERE json_stale
                   AND archived_at IS NOT NULL
                   AND archive_relpath IS NOT NULL
                   AND confirmation = 'confirmed'
                 ORDER BY archived_at
                 LIMIT $1
            """, limit)
        return [dict(row) for row in rows]

    async def copies_overdue(self, *, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Archived this long ago and still not copied (SRS-ARC-14). Something is
        failing every pass, so it is raised rather than retried in silence.
        """
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT session_id, archived_at FROM sessions
                 WHERE archived_at < now() - make_interval(hours => $1)
                   AND copied_at IS NULL
                   AND quarantine_reason IS NULL
                   AND confirmation = 'confirmed'
                 ORDER BY archived_at
                 LIMIT 50
            """, hours)
        return [dict(row) for row in rows]

    async def mark_json_stale(self, session_id: str) -> bool:
        """A prescription arrived for a recording already archived."""
        async with self._acquire() as conn:
            updated = await conn.fetchval("""
                UPDATE sessions SET json_stale = true, updated_at = now()
                 WHERE session_id = $1 AND archived_at IS NOT NULL
                RETURNING session_id
            """, session_id)
        return updated is not None

    async def next_copy_version(self, session_id: str, kind: str) -> int:
        async with self._acquire() as conn:
            return int(await conn.fetchval("""
                SELECT COALESCE(MAX(version), 0) + 1 FROM cloud_copies
                 WHERE session_id = $1 AND kind = $2
            """, session_id, kind))

    async def record_cloud_copy(
        self, *, session_id: str, kind: str, object_key: str, version: int,
        byte_length: int, sha256: bytes, plain_sha256: Optional[bytes],
    ) -> int:
        """
        One verified object in the copy bucket, recorded only after the worker
        has read it back and matched it (SRS-ARC-09, steps 5 and 6).

        Recording the same object twice - a worker stopped between the upload
        and this call - updates the row instead of failing, so the retry
        finishes rather than blocking on its own earlier attempt (AT-65).
        """
        async with self._acquire() as conn:
            return int(await conn.fetchval("""
                INSERT INTO cloud_copies
                    (session_id, kind, object_key, version, bytes, sha256,
                     plain_sha256, verified_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7, now())
                ON CONFLICT (object_key) DO UPDATE
                    SET bytes = EXCLUDED.bytes, sha256 = EXCLUDED.sha256,
                        plain_sha256 = EXCLUDED.plain_sha256,
                        uploaded_at = now(), verified_at = now()
                RETURNING id
            """, session_id, kind, object_key, version, byte_length, sha256,
                 plain_sha256))

    async def copies_for(self, session_id: str) -> List[Dict[str, Any]]:
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT kind, object_key, version, bytes, sha256, plain_sha256,
                       uploaded_at, verified_at
                  FROM cloud_copies WHERE session_id = $1
                 ORDER BY kind, version
            """, session_id)
        return [dict(row) for row in rows]

    async def mark_copied(self, session_id: str) -> None:
        """The cloud copy is complete: the audio and its JSON are both verified."""
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE sessions SET copied_at = now(), json_stale = false,
                                    updated_at = now()
                 WHERE session_id = $1
            """, session_id)

    async def clear_json_stale(self, session_id: str) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET json_stale = false, updated_at = now() "
                "WHERE session_id = $1", session_id)

    async def mark_segments_deleted(self, session_id: str) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET segments_deleted_at = now(), updated_at = now() "
                "WHERE session_id = $1", session_id)

    async def session_for_visit(self, visit, hospital_id: Optional[str] = None
                                ) -> Optional[Dict[str, Any]]:
        """
        The recording of this visit, if one was granted and opened. Used when a
        prescription arrives late, to mark its JSON for writing again.

        Only while the recording is still this visit's: after a reassign its
        first grant still names it, but the recording now belongs to the
        patient the prescription was built for, and the wrong patient's late
        records must not be attached to it.
        """
        async with self._acquire() as conn:
            row = await conn.fetchrow("""
                SELECT s.session_id, s.archived_at, s.copied_at
                  FROM grant_authorisations g
                  JOIN sessions s ON s.session_id = g.session_id
                 WHERE (s.grant_jti IS NULL OR s.grant_jti = g.jti
                        OR EXISTS (SELECT 1 FROM session_clicks c
                                    WHERE c.session_id = s.session_id
                                      AND c.grant_jti = g.jti))
                   AND g.patient_id = $1 AND g.doctor_id = $2
                   AND g.cmed_hospital_id = $3 AND g.start_time = $4
                   AND g.visit_date = $5
                   -- A clinic's records attach only to that clinic's recordings.
                   AND ($6::text IS NULL OR s.hospital_id = $6)
                 ORDER BY g.issued_at DESC LIMIT 1
            """, visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
                 visit.start_time, visit.day, hospital_id)
            if row is None:
                # Clicked during a recording whose grant never came (held, or
                # the server was unreachable): the marker itself names the visit.
                row = await conn.fetchrow("""
                    SELECT s.session_id, s.archived_at, s.copied_at
                      FROM session_clicks c
                      JOIN sessions s ON s.session_id = c.session_id
                     WHERE c.patient_id = $1 AND c.doctor_id = $2
                       AND c.cmed_hospital_id = $3 AND c.start_time = $4
                       AND c.visit_date = $5
                       AND ($6::text IS NULL OR s.hospital_id = $6)
                     ORDER BY c.received_at DESC LIMIT 1
                """, visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
                     visit.start_time, visit.day, hospital_id)
        return dict(row) if row else None

    # ============================================================
    # Purge receipts
    # ============================================================

    async def store_receipt(
        self, *, session_id: str, scope: str, seq_no: Optional[int],
        sha256: bytes, payload: Dict[str, Any], signature: bytes,
    ) -> None:
        async with self._acquire() as conn:
            await conn.execute("""
                INSERT INTO purge_receipts
                    (session_id, scope, seq_no, sha256, payload, signature)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT DO NOTHING
            """, session_id, scope, seq_no, sha256,
                 json.dumps(payload, ensure_ascii=False), signature)

    async def receipts_for(self, session_id: str) -> List[Dict[str, Any]]:
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT payload, signature FROM purge_receipts
                 WHERE session_id = $1 ORDER BY COALESCE(seq_no, 0)
            """, session_id)
        return [
            {
                "payload": json.loads(row["payload"]) if isinstance(row["payload"], str)
                           else row["payload"],
                "signature": bytes(row["signature"]).hex(),
            }
            for row in rows
        ]

    # ============================================================
    # Audit and alerts
    # ============================================================

    async def audit(
        self, *, event_type: str, actor_type: str, actor_id: Optional[str] = None,
        device_id=None, session_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Append one hash-chained audit entry.

        Serialised by an advisory lock: without it two concurrent writers can read
        the same prev_hash and fork the chain, which silently destroys its value.
        """
        detail = detail or {}
        occurred_at = _utcnow()

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", AUDIT_LOCK_ID)
                prev = await conn.fetchval(
                    "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1")

                entry_hash = integrity.audit_entry_hash(
                    prev_hash=bytes(prev) if prev else None,
                    occurred_at=occurred_at,
                    event_type=event_type,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    session_id=session_id,
                    detail=detail,
                )

                await conn.execute("""
                    INSERT INTO audit_log
                        (occurred_at, event_type, actor_type, actor_id, device_id,
                         session_id, detail, prev_hash, entry_hash)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """, occurred_at, event_type, actor_type, actor_id, device_id,
                     session_id, json.dumps(detail, ensure_ascii=False),
                     prev, entry_hash)

    # ---- a resumed recording continues the one before it (S4) ----

    async def set_continuation(self, session_id: str, continues_session_id: str) -> None:
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE sessions SET continues_session_id = $2, updated_at = now()
                 WHERE session_id = $1 AND continues_session_id IS NULL
            """, session_id, continues_session_id)

    async def continuation_links(self, session_id: str) -> Dict[str, Any]:
        """The recording this one continues, and the one that continues it."""
        async with self._acquire() as conn:
            before = await conn.fetchrow("""
                SELECT p.session_id, p.file_stem FROM sessions s
                  JOIN sessions p ON p.session_id = s.continues_session_id
                 WHERE s.session_id = $1
            """, session_id)
            after = await conn.fetchrow("""
                SELECT session_id, file_stem FROM sessions
                 WHERE continues_session_id = $1 ORDER BY opened_at LIMIT 1
            """, session_id)
        return {"continues": dict(before) if before else None,
                "continued_by": dict(after) if after else None}

    async def continuations_of(self, session_id: str) -> List[str]:
        async with self._acquire() as conn:
            rows = await conn.fetch(
                "SELECT session_id FROM sessions WHERE continues_session_id = $1", session_id)
        return [r["session_id"] for r in rows]

    # ---- alerts that must not repeat ----

    async def alert_raised_since(self, alert_type: str, device_id, since: datetime) -> bool:
        async with self._acquire() as conn:
            return bool(await conn.fetchval("""
                SELECT EXISTS (SELECT 1 FROM integrity_alerts
                                WHERE alert_type = $1 AND device_id = $2 AND raised_at >= $3)
            """, alert_type, device_id, since))

    async def group_alert(self, *, alert_type: str, group: str, session_id: str,
                          window: timedelta, severity: str = "warning", device_id=None,
                          detail: Optional[Dict[str, Any]] = None) -> bool:
        """
        One open alert per group and window, counting what it covers, rather
        than one per recording: a CMED outage made one unconfirmed_recording
        alert for every consultation and buried everything else (D4). Returns
        True when a new alert was raised.
        """
        async with self._acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT id FROM integrity_alerts
                     WHERE alert_type = $1 AND detail->>'group' = $2
                       AND resolved_at IS NULL AND raised_at > now() - $3::interval
                     ORDER BY id DESC LIMIT 1 FOR UPDATE
                """, alert_type, group, window)
                if row is not None:
                    await conn.execute("""
                        UPDATE integrity_alerts
                           SET detail = jsonb_set(
                                 jsonb_set(detail, '{count}',
                                           to_jsonb(COALESCE((detail->>'count')::int, 0) + 1)),
                                 '{session_ids}',
                                 CASE WHEN jsonb_array_length(COALESCE(detail->'session_ids',
                                                                       '[]')) < 200
                                      THEN COALESCE(detail->'session_ids', '[]') || to_jsonb($2::text)
                                      ELSE detail->'session_ids' END)
                         WHERE id = $1
                    """, row["id"], session_id)
                    return False
                await conn.execute("""
                    INSERT INTO integrity_alerts (session_id, device_id, alert_type, severity,
                                                  detail)
                    VALUES ($1,$2,$3,$4,$5)
                """, session_id, device_id, alert_type, severity,
                     json.dumps({**(detail or {}), "group": group, "count": 1,
                                 "session_ids": [session_id]}, ensure_ascii=False))
        logger.warning("Integrity alert [%s/%s] group=%s", severity, alert_type, group)
        return True

    async def raise_alert(
        self, *, alert_type: str, severity: str = "warning",
        session_id: Optional[str] = None, device_id=None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._acquire() as conn:
            await conn.execute("""
                INSERT INTO integrity_alerts
                    (session_id, device_id, alert_type, severity, detail)
                VALUES ($1,$2,$3,$4,$5)
            """, session_id, device_id, alert_type, severity,
                 json.dumps(detail or {}, ensure_ascii=False))
        logger.warning("Integrity alert [%s/%s] session=%s: %s",
                       severity, alert_type, session_id, detail)

    async def store_health(self, device_id, entries: List[Dict[str, Any]]) -> List[int]:
        """
        A recorder's health log, as it kept it while offline. Returns the seqs
        stored for the first time: a resent batch stores nothing twice, and
        only new alerts are raised.
        """
        async with self._acquire() as conn:
            async with conn.transaction():
                new = []
                for e in entries:
                    seq = await conn.fetchval("""
                        INSERT INTO device_health_log (device_id, seq, kind, occurred_at, data)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (device_id, seq) DO NOTHING
                        RETURNING seq
                    """, device_id, e["seq"], e["kind"], e["at"],
                         json.dumps(e["data"], ensure_ascii=False))
                    if seq is not None:
                        new.append(int(seq))
                return new

    # ============================================================
    # v3 (SRS 3.2): clinic mapping, grants, confirmation, refusals and
    # Channel B. Tables from scripts/010_v3_confirmation_channel_b.sql.
    # ============================================================

    async def hospital_for_cmed_id(self, cmed_hospital_id: str) -> Optional[str]:
        async with self._acquire() as conn:
            return await conn.fetchval(
                "SELECT hospital_id FROM hospitals WHERE cmed_hospital_id = $1",
                cmed_hospital_id)

    async def active_clinic_for_cmed(self, cmed_hospital_id: str) -> Optional[str]:
        """
        Our clinic for CMED's code, if it is one we run: mapped through
        cmed_hospital_id, or CMED already using our own code (as API 2 allows).
        """
        async with self._acquire() as conn:
            return await conn.fetchval("""
                SELECT hospital_id FROM hospitals
                 WHERE active AND (cmed_hospital_id = $1
                                   OR (hospital_id = $1 AND cmed_hospital_id IS NULL))
                 ORDER BY (cmed_hospital_id = $1) DESC NULLS LAST
                 LIMIT 1
            """, cmed_hospital_id)

    async def alert_once(self, *, alert_type: str, group: str, window: timedelta,
                         severity: str = "warning",
                         detail: Optional[Dict[str, Any]] = None) -> bool:
        """
        One open alert per group and window, its detail kept current: for a
        state that holds across many sweeps (a clinic CMED has gone quiet for),
        which would otherwise be raised every 30 seconds.
        """
        payload = json.dumps({**(detail or {}), "group": group}, ensure_ascii=False,
                             default=str)
        async with self._acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT id FROM integrity_alerts
                     WHERE alert_type = $1 AND detail->>'group' = $2
                       AND resolved_at IS NULL AND raised_at > now() - $3::interval
                     ORDER BY id DESC LIMIT 1 FOR UPDATE
                """, alert_type, group, window)
                if row is not None:
                    await conn.execute("UPDATE integrity_alerts SET detail = $2 WHERE id = $1",
                                       row["id"], payload)
                    return False
                await conn.execute("""
                    INSERT INTO integrity_alerts (alert_type, severity, detail)
                    VALUES ($1, $2, $3)
                """, alert_type, severity, payload)
        logger.warning("Integrity alert [%s/%s] group=%s", severity, alert_type, group)
        return True

    async def hospital_exists(self, hospital_id: str) -> bool:
        async with self._acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM hospitals WHERE hospital_id = $1", hospital_id))

    async def set_cmed_hospital_id(self, hospital_id: str,
                                   cmed_hospital_id: Optional[str]) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE hospitals SET cmed_hospital_id = $2 WHERE hospital_id = $1",
                hospital_id, cmed_hospital_id or None)

    async def create_authorisation(self, *, jti: str, device_id, hospital_id: str,
                                   visit, expires_at: datetime,
                                   visit_id: Optional[str] = None) -> None:
        async with self._acquire() as conn:
            await conn.execute("""
                INSERT INTO grant_authorisations
                    (jti, device_id, hospital_id, cmed_hospital_id, patient_id,
                     doctor_id, start_time, visit_date, expires_at, visit_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """, jti, device_id, hospital_id, visit.cmed_hospital_id, visit.patient_id,
                 visit.doctor_id, visit.start_time, visit.day, expires_at, visit_id)

    async def set_session_visit(self, session_id: str, visit_id: str) -> None:
        async with self._acquire() as conn:
            await conn.execute("UPDATE sessions SET visit_id = $2 WHERE session_id = $1 "
                               "AND visit_id IS DISTINCT FROM $2", session_id, visit_id)

    async def note_prescription(self, session_id: str) -> None:
        """CMED's API 3 for this recording arrived. Once: a correction moves nothing."""
        async with self._acquire() as conn:
            await conn.execute("UPDATE sessions SET prescription_received_at = now() "
                               "WHERE session_id = $1 AND prescription_received_at IS NULL",
                               session_id)

    async def prescription_notice(self, device_id: str, session_id: str
                                  ) -> Optional[float]:
        """
        Seconds since API 3 arrived for this PC's open recording, or None: what
        the heartbeat reply tells the recorder.
        """
        async with self._acquire() as conn:
            ago = await conn.fetchval("""
                SELECT EXTRACT(EPOCH FROM now() - prescription_received_at)
                  FROM sessions
                 WHERE session_id = $1 AND device_id = $2 AND closed_at IS NULL
                   AND prescription_received_at IS NOT NULL
            """, session_id, device_id)
        return None if ago is None else max(0.0, float(ago))

    async def get_authorisation(self, jti: str) -> Optional[Dict[str, Any]]:
        async with self._acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM grant_authorisations WHERE jti = $1", jti)
        return dict(row) if row else None

    async def grants_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        """Every grant linked to this recording, oldest first."""
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM grant_authorisations WHERE session_id = $1
                 ORDER BY issued_at
            """, session_id)
        return [dict(r) for r in rows]

    async def reassign_session(self, session_id: str, *, patient_id: str,
                               doctor_id: str) -> None:
        """The recording now belongs to the patient its prescription was built for."""
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE sessions SET patient_id = $2, doctor_id = $3, updated_at = now()
                 WHERE session_id = $1
            """, session_id, patient_id, doctor_id)

    async def link_authorisation(self, jti: str, session_id: str) -> None:
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE grant_authorisations SET session_id = $2
                 WHERE jti = $1 AND (session_id IS NULL OR session_id = $2)
            """, jti, session_id)

    _VISIT_MATCH = """patient_id = $1 AND doctor_id = $2 AND cmed_hospital_id = $3
                      AND start_time = $4 AND visit_date = $5 AND hospital_id = $6"""

    async def find_unclaimed_notice(self, visit, *, hospital_id: str,
                                    since: datetime) -> Optional[int]:
        """The most recent unused API 2 for this visit and clinic (SRS-CNF-04, -05)."""
        async with self._acquire() as conn:
            return await conn.fetchval(f"""
                SELECT id FROM confirmation_notices
                 WHERE claimed_by_jti IS NULL
                   AND {self._VISIT_MATCH} AND received_at >= $7
                 ORDER BY received_at DESC, id DESC LIMIT 1
            """, visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
                 visit.start_time, visit.day, hospital_id, since)

    async def find_waiting_authorisation(self, visit, *, hospital_id: str,
                                         since: datetime) -> Optional[Dict[str, Any]]:
        """The most recent grant for this visit that no API 2 has confirmed yet."""
        async with self._acquire() as conn:
            row = await conn.fetchrow(f"""
                SELECT jti, session_id FROM grant_authorisations
                 WHERE notice_id IS NULL AND {self._VISIT_MATCH} AND issued_at >= $7
                 ORDER BY issued_at DESC LIMIT 1
            """, visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
                 visit.start_time, visit.day, hospital_id, since)
        return dict(row) if row else None

    async def visit_devices(self, visit, *, hospital_id: str) -> List[str]:
        """Every PC that asked to record this consultation."""
        async with self._acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT DISTINCT device_id::text AS device_id FROM grant_authorisations
                 WHERE {self._VISIT_MATCH}
            """, visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
                 visit.start_time, visit.day, hospital_id)
        return [r["device_id"] for r in rows]

    async def claim_notice(self, notice_id: int, jti: str) -> bool:
        """One API 2 confirms one grant, once (SRS-CNF-04). All or nothing."""
        async with self._acquire() as conn:
            async with conn.transaction():
                grant = await conn.fetchval("""
                    SELECT jti FROM grant_authorisations
                     WHERE jti = $1 AND notice_id IS NULL FOR UPDATE
                """, jti)
                if grant is None:
                    return False
                claimed = await conn.fetchval("""
                    UPDATE confirmation_notices SET claimed_by_jti = $2
                     WHERE id = $1 AND claimed_by_jti IS NULL RETURNING id
                """, notice_id, jti)
                if claimed is None:
                    return False
                await conn.execute(
                    "UPDATE grant_authorisations SET notice_id = $1 WHERE jti = $2",
                    notice_id, jti)
        return True

    async def set_session_confirmation(self, session_id: str, state: str, *,
                                       grant_jti: Optional[str] = None) -> None:
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE sessions
                   SET confirmation = $2::text,
                       -- An archived recording's JSON carries its marking:
                       -- written again when the marking changes.
                       json_stale = json_stale OR (archived_at IS NOT NULL
                                                   AND confirmation IS DISTINCT FROM $2::text),
                       grant_jti = COALESCE($3, grant_jti),
                       unconfirmed_at = CASE WHEN $2::text = 'unconfirmed'
                                             THEN COALESCE(unconfirmed_at, now())
                                             ELSE unconfirmed_at END,
                       updated_at = now()
                 WHERE session_id = $1
            """, session_id, state, grant_jti)

    async def session_confirmation(self, session_id: str) -> Optional[Dict[str, Any]]:
        async with self._acquire() as conn:
            row = await conn.fetchrow("""
                SELECT session_id, confirmation, opened_at, grant_jti, unconfirmed_at,
                       refused_at, device_id, doctor_id, hospital_id, session_date
                  FROM sessions WHERE session_id = $1
            """, session_id)
        return dict(row) if row else None

    async def reconcile_pending_confirmations(self, limit: int = 200) -> List[str]:
        """
        Sessions that are waiting although their grant already holds CMED's
        notice: confirm them.

        This is the backstop for the two sides crossing - Channel B claiming
        the notice at the same moment the recording opens - and it is cheap
        enough to run on every sweep.
        """
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                UPDATE sessions s
                   SET confirmation = 'confirmed', unconfirmed_at = NULL,
                       updated_at = now()
                  FROM grant_authorisations g
                 WHERE g.jti = s.grant_jti
                   AND g.notice_id IS NOT NULL
                   AND s.confirmation IN ('pending', 'unconfirmed')
                   AND s.session_id IN (
                       SELECT session_id FROM sessions
                        WHERE confirmation IN ('pending', 'unconfirmed')
                          AND grant_jti IS NOT NULL
                        ORDER BY opened_at LIMIT $1)
                RETURNING s.session_id
            """, limit)
        return [r["session_id"] for r in rows]

    async def set_erase_deadlines(self, days: int) -> int:
        """Every recording CMED has not confirmed gets its date (opened + days)."""
        async with self._acquire() as conn:
            result = await conn.execute("""
                UPDATE sessions
                   SET erase_after = opened_at + make_interval(days => $1)
                 WHERE erase_after IS NULL
                   AND confirmation IN ('pending', 'unconfirmed', 'legacy')
                   AND protocol_version >= 2
                   AND opened_at IS NOT NULL
                   AND NOT legal_hold
            """, days)
        return int(result.split()[-1]) if result else 0

    async def sessions_due_for_erasure(self, *, limit: int,
                                       silent_after: timedelta) -> Dict[str, Any]:
        """
        Past their erase_after and still not confirmed. Those of a clinic CMED
        has sent nothing for in `silent_after` are held back and counted per
        clinic instead: during an outage nothing is erased.
        """
        async with self._acquire() as conn:
            due = await conn.fetch("""
                SELECT s.session_id, s.hospital_id, s.confirmation, s.opened_at,
                       s.device_id
                  FROM sessions s
                  JOIN hospitals h ON h.hospital_id = s.hospital_id
                 WHERE s.erase_after < now()
                   AND s.confirmation IN ('pending', 'unconfirmed', 'legacy')
                   AND NOT s.legal_hold
                   AND h.cmed_last_message_at > now() - $2::interval
                 ORDER BY s.erase_after
                 LIMIT $1
            """, limit, silent_after)
            held = await conn.fetch("""
                SELECT s.hospital_id, count(*) AS n, max(h.cmed_last_message_at) AS last_heard
                  FROM sessions s
                  LEFT JOIN hospitals h ON h.hospital_id = s.hospital_id
                 WHERE s.erase_after < now()
                   AND s.confirmation IN ('pending', 'unconfirmed', 'legacy')
                   AND NOT s.legal_hold
                   AND (h.cmed_last_message_at IS NULL
                        OR h.cmed_last_message_at <= now() - $1::interval)
                 GROUP BY s.hospital_id
            """, silent_after)
        return {"due": [dict(r) for r in due], "held": [dict(r) for r in held]}

    async def erase_share(self, hospital_ids: List[str], *, days: int,
                          window_days: int = 7) -> Dict[str, Dict[str, int]]:
        """
        Per clinic, of the recordings opened in the week now coming due for
        erasure: how many are still unconfirmed, and how many there were. The
        circuit breaker (030) compares the two.
        """
        if not hospital_ids:
            return {}
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT hospital_id,
                       count(*) FILTER (WHERE confirmation IN ('pending', 'unconfirmed', 'legacy')
                                          AND NOT legal_hold) AS unconfirmed,
                       count(*) AS total
                  FROM sessions
                 WHERE hospital_id = ANY($1::text[])
                   AND protocol_version >= 2
                   AND opened_at >= now() - make_interval(days => $2::int + $3::int)
                   AND opened_at <  now() - make_interval(days => $2::int) + interval '1 day'
                 GROUP BY hospital_id
            """, hospital_ids, days, window_days)
        return {r["hospital_id"]: {"unconfirmed": int(r["unconfirmed"]), "total": int(r["total"])}
                for r in rows}

    async def erase_released(self, hospital_id: str) -> Optional[Dict[str, Any]]:
        """An admin's release of this clinic's breaker that still holds, if any."""
        async with self._acquire() as conn:
            row = await conn.fetchrow("""
                SELECT released_by, reason, released_at, until FROM erase_releases
                 WHERE hospital_id = $1 AND until > now()
                 ORDER BY until DESC LIMIT 1
            """, hospital_id)
        return dict(row) if row else None

    async def release_erase(self, hospital_id: str, *, by: str, reason: str,
                            hours: int) -> Dict[str, Any]:
        async with self._acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO erase_releases (hospital_id, released_by, reason, until)
                VALUES ($1, $2, $3, now() + make_interval(hours => $4))
                RETURNING hospital_id, released_by, reason, released_at, until
            """, hospital_id, by, reason, hours)
        return dict(row)

    async def erasing_soon(self, *, within: timedelta) -> List[Dict[str, Any]]:
        """Per clinic: recordings that will be erased within `within` unless confirmed."""
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT hospital_id, count(*) AS n, min(erase_after) AS first
                  FROM sessions
                 WHERE erase_after BETWEEN now() AND now() + $1::interval
                   AND confirmation IN ('pending', 'unconfirmed', 'legacy')
                   AND NOT legal_hold
                 GROUP BY hospital_id
            """, within)
        return [dict(r) for r in rows]

    async def sessions_in_confirmation(self, states: List[str], *,
                                       opened_before: datetime) -> List[Dict[str, Any]]:
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT session_id, confirmation, opened_at, device_id
                  FROM sessions
                 WHERE confirmation = ANY($1::text[]) AND opened_at < $2
                 ORDER BY opened_at LIMIT 500
            """, list(states), opened_before)
        return [dict(r) for r in rows]

    async def record_refusal(self, *, session_id: str, device_id, hospital_id: str,
                             doctor_id: Optional[str],
                             visit_sha256: Optional[bytes],
                             signed: Optional[bool] = None) -> bool:
        """True the first time a session is refused; a retry changes nothing."""
        async with self._acquire() as conn:
            inserted = await conn.fetchval("""
                INSERT INTO session_refusals
                    (session_id, device_id, hospital_id, doctor_id, visit_sha256, signed)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (session_id) DO NOTHING RETURNING session_id
            """, session_id, device_id, hospital_id, doctor_id, visit_sha256, signed)
        return inserted is not None

    async def refusals_since(self, device_id, since: datetime) -> int:
        async with self._acquire() as conn:
            return int(await conn.fetchval("""
                SELECT count(*) FROM session_refusals
                 WHERE device_id = $1 AND refused_at >= $2
            """, device_id, since))

    async def mark_late_alerted(self, session_id: str) -> bool:
        """True once per recording: the missing-clips alert is raised one time."""
        async with self._acquire() as conn:
            return bool(await conn.fetchval("""
                UPDATE sessions SET late_alerted_at = now()
                 WHERE session_id = $1 AND late_alerted_at IS NULL
                RETURNING 1
            """, session_id))

    # ---- the files an erased recording leaves behind ----

    async def erase_task(self, session_id: str) -> Optional[Dict[str, Any]]:
        async with self._acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM erase_tasks WHERE session_id = $1",
                                      session_id)
        return _erase_row(row) if row else None

    async def pending_erase(self, limit: int = 20) -> List[Dict[str, Any]]:
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM erase_tasks WHERE completed_at IS NULL
                 ORDER BY created_at LIMIT $1
            """, limit)
        return [_erase_row(r) for r in rows]

    async def set_erase_cloud_keys(self, session_id: str, keys: List[Dict[str, Any]]) -> None:
        async with self._acquire() as conn:
            await conn.execute("UPDATE erase_tasks SET cloud_keys = $2 WHERE session_id = $1",
                               session_id, json.dumps(keys))

    async def complete_erase(self, session_id: str) -> bool:
        """The files are gone: nothing naming the patient is left in the task."""
        async with self._acquire() as conn:
            result = await conn.execute("""
                UPDATE erase_tasks SET completed_at = now(), files = '[]'
                 WHERE session_id = $1 AND completed_at IS NULL
                   AND jsonb_array_length(cloud_keys) = 0
            """, session_id)
        return bool(result) and result.split()[-1] != "0"

    async def refusal(self, session_id: str) -> Optional[Dict[str, Any]]:
        async with self._acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM session_refusals WHERE session_id = $1", session_id)
        return dict(row) if row else None

    async def complete_refusal(self, session_id: str) -> None:
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE session_refusals SET completed_at = COALESCE(completed_at, now())
                 WHERE session_id = $1
            """, session_id)

    async def refused_visit(self, visit_sha256: bytes) -> bool:
        async with self._acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM session_refusals WHERE visit_sha256 = $1", visit_sha256))

    async def erase_session(self, session_id: str, *, state: str) -> None:
        """
        Remove a consultation's content and keep only its bare row, without the
        patient, so the audit log still resolves (SRS-CNS-06, SRS-CNF-09).
        The caller has already removed the objects from storage.
        """
        async with self._acquire() as conn:
            async with conn.transaction():
                # An archived recording has files on the server and copies in
                # the cloud. Their names are written to an erase task for the
                # archive worker (and the cloud keys for this server), and then
                # everything else that names the patient goes from the row.
                session = await conn.fetchrow(
                    "SELECT archive_relpath FROM sessions WHERE session_id = $1", session_id)
                files = []
                if session is not None and session["archive_relpath"]:
                    files.append(session["archive_relpath"])
                for part in await conn.fetch(
                        "SELECT archive_relpath FROM consultation_parts "
                        " WHERE session_id = $1 AND archive_relpath IS NOT NULL", session_id):
                    files.append(part["archive_relpath"])
                cloud = [{"kind": r["kind"], "object_key": r["object_key"]}
                         for r in await conn.fetch(
                             "SELECT kind, object_key FROM cloud_copies WHERE session_id = $1",
                             session_id)]
                if files or cloud:
                    await conn.execute("""
                        INSERT INTO erase_tasks (session_id, files, cloud_keys)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (session_id) DO UPDATE
                           SET files = (SELECT jsonb_agg(DISTINCT f) FROM jsonb_array_elements(
                                            erase_tasks.files || EXCLUDED.files) f),
                               cloud_keys = erase_tasks.cloud_keys || EXCLUDED.cloud_keys,
                               completed_at = NULL
                    """, session_id, json.dumps(sorted(set(files))), json.dumps(cloud))
                await conn.execute("DELETE FROM purge_receipts WHERE session_id = $1",
                                   session_id)
                await conn.execute("DELETE FROM segments WHERE session_id = $1", session_id)
                await conn.execute("DELETE FROM chain_entries WHERE session_id = $1",
                                   session_id)
                await conn.execute("DELETE FROM session_clicks WHERE session_id = $1",
                                   session_id)
                await conn.execute("DELETE FROM consultation_parts WHERE session_id = $1",
                                   session_id)
                await conn.execute("DELETE FROM cloud_copies WHERE session_id = $1",
                                   session_id)
                await conn.execute("""
                    UPDATE grant_authorisations SET patient_id = 'REDACTED'
                     WHERE session_id = $1
                """, session_id)
                await conn.execute("""
                    UPDATE sessions
                       SET patient_id = 'REDACTED', object_prefix = NULL, manifest = NULL,
                           file_stem = NULL, archive_relpath = NULL, parts_count = 1,
                           status = $2::text, confirmation = $2::text,
                           refused_at = CASE WHEN $2::text = 'refused' THEN now()
                                             ELSE refused_at END,
                           updated_at = now()
                     WHERE session_id = $1
                """, session_id, state)
            # The old clip table may still name this consultation's audio.
            # Not every database has it, so this is best effort, in a
            # savepoint: inside a recording's transaction a failed statement
            # would otherwise abort everything after it. (ner_results and
            # transcripts went in 022_v3_drop_v1_tables.sql.)
            for table in ("clips",):
                try:
                    async with conn.transaction():
                        await conn.execute(f"DELETE FROM {table} WHERE session_id = $1",
                                           session_id)
                except Exception as exc:
                    logger.warning("Could not clear %s for %s: %s", table, session_id, exc)

    async def delete_notices_for(self, visit) -> int:
        """A refused visit's API 2 notices go too (SRS-CNS-04)."""
        async with self._acquire() as conn:
            result = await conn.execute("""
                DELETE FROM confirmation_notices
                 WHERE patient_id = $1 AND doctor_id = $2 AND cmed_hospital_id = $3
                   AND start_time = $4 AND visit_date = $5
            """, visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
                 visit.start_time, visit.day)
        return int(result.split()[-1]) if result else 0

    # ---- Channel B ----

    async def cmed_key_valid(self, key_sha256: bytes) -> Optional[str]:
        found = await self.cmed_key_lookup(key_sha256)
        return found["label"] if found else None

    async def cmed_key_lookup(self, key_sha256: bytes) -> Optional[Dict[str, Any]]:
        """A live static CMED key: its label and the hospitals it may speak for."""
        async with self._acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE api_keys SET last_used_at = now()
                 WHERE key_sha256 = $1 AND scope = 'cmed' AND revoked_at IS NULL
                   AND (expires_at IS NULL OR expires_at > now())
                RETURNING label, cmed_hospital_ids
            """, key_sha256)
        return dict(row) if row else None

    async def cmed_token_lookup(self, token_sha256: bytes) -> Optional[Dict[str, Any]]:
        """A live access token, with its client's address list."""
        async with self._acquire() as conn:
            row = await conn.fetchrow("""
                SELECT t.client_id, t.cmed_hospital_ids, t.scopes, c.allowed_cidrs
                  FROM cmed_access_tokens t JOIN cmed_clients c USING (client_id)
                 WHERE t.token_sha256 = $1 AND t.revoked_at IS NULL AND t.expires_at > now()
                   AND c.revoked_at IS NULL AND c.expires_at > now()
            """, token_sha256)
        return dict(row) if row else None

    async def create_cmed_key(self, *, label: str, created_by: str,
                              expires_at: Optional[datetime],
                              cmed_hospital_ids: Optional[List[str]] = None) -> str:
        """Returned once. Only the hash is stored (SRS-CHB-02)."""
        key = "cmed_" + new_token()
        async with self._acquire() as conn:
            await conn.execute("""
                INSERT INTO api_keys (key_sha256, label, scope, created_by, expires_at,
                                      cmed_hospital_ids)
                VALUES ($1, $2, 'cmed', $3, $4, $5)
            """, hash_token(key), label, created_by, expires_at, cmed_hospital_ids)
        return key

    async def revoke_cmed_keys(self, label: str) -> int:
        async with self._acquire() as conn:
            result = await conn.execute("""
                UPDATE api_keys SET revoked_at = now()
                 WHERE scope = 'cmed' AND label = $1 AND revoked_at IS NULL
            """, label)
        return int(result.split()[-1]) if result else 0

    async def record_notice(self, visit, *, hospital_id: str,
                            clinical_record_id: int, latest: bool = True) -> int:
        """
        The five fields of a visit CMED's server told us about - by API 2, or
        by API 3 when API 2 was lost or is late - kept for matching
        (SRS-CNF-03). One notice per visit: the second message to arrive
        returns the first one's id, pointing it at its own record when it is
        the newer API 2 (`latest`) - the most recent API 2 is the one used
        (SRS-CNF-05). The body is not here: it is in aims_clinical, which this
        database never reads.
        """
        async with self._acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                                   "|".join((visit.cmed_hospital_id, visit.patient_id,
                                             visit.doctor_id, visit.start_time,
                                             str(visit.day))))
                existing = await conn.fetchval("""
                    SELECT id FROM confirmation_notices
                     WHERE patient_id = $1 AND doctor_id = $2 AND cmed_hospital_id = $3
                       AND start_time = $4 AND visit_date = $5
                       AND hospital_id IS NOT DISTINCT FROM $6
                     ORDER BY id LIMIT 1
                """, visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
                     visit.start_time, visit.day, hospital_id)
                if existing is not None:
                    if latest:
                        await conn.execute("UPDATE confirmation_notices "
                                           "SET clinical_record_id = $2 WHERE id = $1",
                                           existing, clinical_record_id)
                    return int(existing)
                return int(await conn.fetchval("""
                    INSERT INTO confirmation_notices
                        (cmed_hospital_id, hospital_id, patient_id, doctor_id, start_time,
                         visit_date, clinical_record_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id
                """, visit.cmed_hospital_id, hospital_id, visit.patient_id, visit.doctor_id,
                     visit.start_time, visit.day, clinical_record_id))

    async def cmed_heard_from(self, hospital_id: str) -> None:
        """CMED's server sent something for this clinic: it is not in an outage."""
        async with self._acquire() as conn:
            await conn.execute("UPDATE hospitals SET cmed_last_message_at = now() "
                               "WHERE hospital_id = $1", hospital_id)

    # ============================================================
    # Consultation parts: who each stretch of a recording belongs to
    # ============================================================

    async def record_click(self, session_id: str, *, entry_no: int, event: str,
                           audio_seconds: float, occurred_at: datetime, visit,
                           grant_jti: Optional[str]) -> None:
        """One marker from the chain. A redelivered marker is stored once."""
        async with self._acquire() as conn:
            await conn.execute("""
                INSERT INTO session_clicks
                    (session_id, entry_no, event, audio_seconds, occurred_at,
                     patient_id, doctor_id, cmed_hospital_id, start_time, visit_date,
                     grant_jti)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (session_id, entry_no) DO UPDATE
                   SET grant_jti = COALESCE(session_clicks.grant_jti, EXCLUDED.grant_jti)
            """, session_id, entry_no, event, audio_seconds, occurred_at,
                 visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
                 visit.start_time, visit.day, grant_jti)

    async def clicks_for(self, session_id: str) -> List[Dict[str, Any]]:
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT entry_no, event, audio_seconds, occurred_at, patient_id,
                       doctor_id, cmed_hospital_id, start_time, visit_date, grant_jti
                  FROM session_clicks WHERE session_id = $1
                 ORDER BY audio_seconds, entry_no
            """, session_id)
        return [dict(r) for r in rows]

    async def parts_for(self, session_id: str) -> List[Dict[str, Any]]:
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM consultation_parts WHERE session_id = $1 ORDER BY part_no
            """, session_id)
        return [dict(r) for r in rows]

    async def store_parts(self, session_id: str, parts: List[Dict[str, Any]], *,
                          keep_archive: bool) -> None:
        """
        The verdict for one recording. `keep_archive`: the cut is unchanged, so
        the files already archived still stand and only the labels move.
        """
        async with self._acquire() as conn:
            async with conn.transaction():
                kept = {}
                if keep_archive:
                    for r in await conn.fetch("""
                        SELECT part_no, archive_relpath, archive_sha256, archive_bytes,
                               copied_at
                          FROM consultation_parts WHERE session_id = $1
                    """, session_id):
                        kept[r["part_no"]] = r
                await conn.execute(
                    "DELETE FROM consultation_parts WHERE session_id = $1", session_id)
                for part in parts:
                    old = kept.get(part["part_no"])
                    await conn.execute("""
                        INSERT INTO consultation_parts
                            (session_id, part_no, start_seconds, end_seconds,
                             patient_id, doctor_id, cmed_hospital_id, start_time,
                             visit_date, grant_jti, rule, needs_review, file_stem,
                             local_start, local_end, archive_relpath, archive_sha256,
                             archive_bytes, copied_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                                $16,$17,$18,$19)
                    """, session_id, part["part_no"], part["start_seconds"],
                         part["end_seconds"], part["patient_id"], part["doctor_id"],
                         part["cmed_hospital_id"], part["start_time"], part["visit_date"],
                         part["grant_jti"], part["rule"], part["needs_review"],
                         part["file_stem"], part["local_start"], part["local_end"],
                         old["archive_relpath"] if old else None,
                         old["archive_sha256"] if old else None,
                         old["archive_bytes"] if old else None,
                         old["copied_at"] if old else None)
                await conn.execute("""
                    UPDATE sessions SET parts_count = $2, updated_at = now()
                     WHERE session_id = $1
                """, session_id, len(parts))

    async def set_parts_stale(self, session_id: str, stale: bool) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET parts_stale = $2, updated_at = now() "
                "WHERE session_id = $1", session_id, stale)

    async def label_session(self, session_id: str, *, patient_id: str, doctor_id: str,
                            grant_jti: Optional[str]) -> None:
        """A recording that is one patient's, after the rules: name it theirs."""
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE sessions SET patient_id = $2, doctor_id = $3,
                                    grant_jti = COALESCE($4, grant_jti),
                                    updated_at = now()
                 WHERE session_id = $1
            """, session_id, patient_id, doctor_id, grant_jti)

    async def mark_part_archived(self, session_id: str, part_no: int, *, relpath: str,
                                 sha256: bytes, byte_length: int) -> None:
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE consultation_parts
                   SET archive_relpath = $3, archive_sha256 = $4, archive_bytes = $5
                 WHERE session_id = $1 AND part_no = $2
            """, session_id, part_no, relpath, sha256, byte_length)

    async def mark_part_copied(self, session_id: str, part_no: int) -> bool:
        """Returns True once every part of the recording is copied."""
        async with self._acquire() as conn:
            await conn.execute("""
                UPDATE consultation_parts SET copied_at = now()
                 WHERE session_id = $1 AND part_no = $2
            """, session_id, part_no)
            left = await conn.fetchval("""
                SELECT count(*) FROM consultation_parts
                 WHERE session_id = $1 AND copied_at IS NULL
            """, session_id)
        return int(left) == 0

    async def archived_parts_for(self, session_id: str) -> List[Dict[str, Any]]:
        """The part files a recut will rebuild the recording from."""
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT part_no, archive_relpath FROM consultation_parts
                 WHERE session_id = $1 AND archive_relpath IS NOT NULL
                 ORDER BY part_no
            """, session_id)
        return [dict(r) for r in rows]

    async def pending_recut(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Archived recordings whose labels changed where the files are cut."""
        async with self._acquire() as conn:
            rows = await conn.fetch("""
                SELECT session_id, hospital_id, doctor_id, patient_id, session_date,
                       opened_at, closed_at, sample_rate, channels, sample_width,
                       file_stem, archive_relpath, archive_sha256, archive_bytes
                  FROM sessions
                 WHERE parts_stale AND archived_at IS NOT NULL
                   AND quarantine_reason IS NULL
                 ORDER BY archived_at
                 LIMIT $1
            """, limit)
        return [dict(r) for r in rows]

    async def set_file_names(self, session_id: str, *, file_stem: str, local_start,
                             local_end) -> Optional[str]:
        """
        The recording's name and its times, as columns (SRS-DBA-25). Names are
        unique; two sessions closing in the same second would share one, so the
        second gets the session's tail rather than overwriting the first.
        """
        for candidate in (file_stem, f"{file_stem}_{session_id[-5:]}"):
            try:
                async with self._acquire() as conn:
                    async with conn.transaction():  # a savepoint inside a session_tx
                        await conn.execute("""
                            UPDATE sessions SET file_stem = $2, local_start = $3,
                                   local_end = $4
                             WHERE session_id = $1
                        """, session_id, candidate, local_start, local_end)
                return candidate
            except asyncpg.UniqueViolationError:
                continue
        logger.error("No unique file name for %s", session_id)
        return None


__all__ = ["V2Repository", "hash_token", "new_token"]
