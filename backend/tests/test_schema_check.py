"""
The server notices a migration that was never applied (failure review D1).

Found live: with 013 missing every reassign got 500, and nothing said why.
Each case removes what one migration leaves behind - inside a transaction that
is rolled back - and checks that exactly that migration is named.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import schema_check                             # noqa: E402

try:
    from test_db_integration import dbs, pg     # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

postgres = pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")
pytestmark = pytest.mark.asyncio


class OneConnection:
    """A pool of one, so the check runs inside the test's own transaction."""

    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@asynccontextmanager
async def broken(pool, *statements):
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            # A schema with a migration missing is made by dropping what it
            # added, which the data guard refuses unless allowed - here, for
            # this transaction only, and it is rolled back.
            await conn.execute("SET LOCAL aims.allow_destructive = 'on'")
            for statement in statements:
                await conn.execute(statement)
            yield OneConnection(conn)
        finally:
            await tx.rollback()


@postgres
async def test_a_complete_database_has_nothing_missing(dbs):
    assert await schema_check.missing(dbs.rec, dbs.cli) == []
    assert schema_check.describe([]) == "ok"


@postgres
@pytest.mark.parametrize("statements,migration,what", [
    (["DROP TABLE consultation_parts", "DROP TABLE session_clicks"],
     "015_v3_consultation_parts.sql", "table session_clicks"),
    (["ALTER TABLE chain_entries DROP CONSTRAINT chain_entries_entry_type_check",
      "ALTER TABLE chain_entries ADD CONSTRAINT chain_entries_entry_type_check CHECK "
      "(entry_type IN ('open','segment','pause','resume','reassign','close'))"],
     "015_v3_consultation_parts.sql", "chain entry type 'marker' accepted"),
    (["ALTER TABLE chain_entries DROP CONSTRAINT chain_entries_entry_type_check",
      "ALTER TABLE chain_entries ADD CONSTRAINT chain_entries_entry_type_check CHECK "
      "(entry_type IN ('open','segment','pause','resume','close'))"],
     "013_v3_reassign.sql", "chain entry type 'reassign' accepted"),
    (["DROP TABLE device_health_log"], "014_v3_device_health.sql", "table device_health_log"),
    (["ALTER TABLE sessions DROP COLUMN file_stem CASCADE"],
     "011_v3_file_names.sql", "column sessions.file_stem"),
])
async def test_the_missing_migration_is_named(dbs, statements, migration, what):
    async with broken(dbs.rec, *statements) as pool:
        problems = await schema_check.missing(pool, dbs.cli)
    named = {p["migration"]: p for p in problems}
    assert migration in named and what in named[migration]["missing"]
    assert schema_check.describe(problems).startswith("missing ")


@postgres
async def test_no_check_on_entry_type_at_all_accepts_every_type(dbs):
    async with broken(dbs.rec, "ALTER TABLE chain_entries DROP CONSTRAINT "
                               "chain_entries_entry_type_check") as pool:
        assert await schema_check.missing(pool, dbs.cli) == []


@postgres
async def test_the_clinical_database_is_checked_too(dbs):
    async with broken(dbs.cli, "DROP TABLE prescriptions CASCADE") as pool:
        problems = await schema_check.missing(dbs.rec, pool)
    assert [p["migration"] for p in problems] == ["clinical/001_aims_clinical.sql"]


@postgres
async def test_startup_alerts_and_strict_mode_refuses(dbs, monkeypatch):
    raised = []

    class Repo:
        async def raise_alert(self, **kw):
            raised.append(kw)

    async with broken(dbs.rec, "DROP TABLE device_health_log") as pool:
        monkeypatch.delenv("AIMS_SCHEMA_STRICT", raising=False)
        problems = await schema_check.at_startup(pool, dbs.cli, repo=Repo())
        assert problems and raised[0]["alert_type"] == "schema_missing"
        assert schema_check.STATE.summary() == "missing 014_v3_device_health.sql"

        monkeypatch.setenv("AIMS_SCHEMA_STRICT", "true")
        with pytest.raises(RuntimeError, match="014_v3_device_health.sql"):
            await schema_check.at_startup(pool, dbs.cli, repo=Repo())

    # Applied (here: rolled back to complete) - clears without a restart.
    monkeypatch.delenv("AIMS_SCHEMA_STRICT")
    await schema_check.STATE.check(dbs.rec, dbs.cli, force=True)
    assert schema_check.STATE.summary() == "ok"
