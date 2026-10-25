"""
The data guard (scripts/guard/data_guard.sql), against a real PostgreSQL.

What no update, mistake or stray command may do to the data: drop a table,
a column or a schema, empty a table, or delete more than 5,000 rows in one
statement - unless a migration decided in review allows it, as the table's
owner. The server's own login cannot allow it by setting the switch.

    cd backend && python -m pytest -q tests/test_data_guard.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

GUARD = (Path(__file__).resolve().parent.parent / "scripts" / "guard"
         / "data_guard.sql").read_text(encoding="utf-8")
pytestmark = pytest.mark.asyncio

DB = "aims_guard_test"
DENIED = asyncpg.InsufficientPrivilegeError


@pytest.fixture(scope="module")
def uri():
    server = pgserver.get_server(tempfile.mkdtemp(prefix="aimsguard"), cleanup_mode="stop")
    base = server.get_uri()

    async def build():
        admin = await asyncpg.connect(base)
        await admin.execute(f"DROP DATABASE IF EXISTS {DB}")
        await admin.execute(f"CREATE DATABASE {DB}")
        for role in ("guard_owner", "guard_app"):
            if not await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
                await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'guard-test'")
        await admin.close()
        root = base.rsplit("/", 1)[0]
        target = f"{root}/{DB}"
        conn = await asyncpg.connect(target)
        await conn.execute("""
            GRANT CREATE, USAGE ON SCHEMA public TO guard_owner;
            SET ROLE guard_owner;
            CREATE TABLE sessions (id int PRIMARY KEY, note text);
            INSERT INTO sessions SELECT g, 'x' FROM generate_series(1, 6000) g;
            RESET ROLE;
            GRANT SELECT, INSERT, UPDATE, DELETE ON sessions TO guard_app;
        """)
        await conn.execute(GUARD)
        await conn.execute(GUARD)                     # every update applies it again: safe
        await conn.close()
        return target

    yield asyncio.run(build())
    server.cleanup()


def _as(uri, user):
    root, db = uri.rsplit("/", 1)
    host = root.split("@", 1)[1]
    return asyncpg.connect(f"postgresql://{user}:guard-test@{host}/{db}")


async def _rows(conn):
    return await conn.fetchval("SELECT count(*) FROM sessions")


async def test_nothing_is_dropped_by_an_update(uri):
    conn = await asyncpg.connect(uri)                 # the superuser, as migrate.sh
    try:
        for statement in ("DROP TABLE sessions",
                          "ALTER TABLE sessions DROP COLUMN note",
                          "DROP SCHEMA public CASCADE",
                          "DROP SCHEMA aims_guard CASCADE",
                          "DROP FUNCTION aims_guard.on_delete() CASCADE",
                          "DROP TRIGGER aims_guard_delete ON sessions",
                          "ALTER TABLE sessions DISABLE TRIGGER aims_guard_delete"):
            with pytest.raises(DENIED, match="data guard"):
                await conn.execute(statement)
        assert await _rows(conn) == 6000
        # Temporary tables, indexes and triggers come and go freely.
        await conn.execute("CREATE TEMP TABLE scratch (x int); DROP TABLE scratch")
        await conn.execute("CREATE INDEX guard_ix ON sessions (note); DROP INDEX guard_ix")
    finally:
        await conn.close()


async def test_no_table_is_emptied(uri):
    conn = await asyncpg.connect(uri)
    try:
        with pytest.raises(DENIED, match="TRUNCATE"):
            await conn.execute("TRUNCATE sessions")
        assert await _rows(conn) == 6000
    finally:
        await conn.close()


async def test_a_delete_whose_where_went_wrong_is_refused(uri):
    app = await _as(uri, "guard_app")
    try:
        with pytest.raises(DENIED, match="would delete 6000 rows"):
            await app.execute("DELETE FROM sessions")
        assert await _rows(app) == 6000
        # An erasure - a few rows - goes through.
        assert await app.execute("DELETE FROM sessions WHERE id <= 300") == "DELETE 300"
        await app.execute("INSERT INTO sessions SELECT g, 'x' FROM generate_series(1, 300) g")
    finally:
        await app.close()


async def test_the_servers_login_cannot_switch_the_guard_off(uri):
    app = await _as(uri, "guard_app")
    try:
        await app.execute("SET aims.allow_destructive = 'on'")
        with pytest.raises(DENIED):
            await app.execute("DELETE FROM sessions")
        with pytest.raises(asyncpg.PostgresError):
            await app.execute("TRUNCATE sessions")
        assert await _rows(app) == 6000
    finally:
        await app.close()


async def test_a_decided_migration_may_as_the_owner(uri):
    owner = await _as(uri, "guard_owner")
    try:
        async with owner.transaction():
            await owner.execute("SET LOCAL aims.allow_destructive = 'on'")
            assert await owner.execute("DELETE FROM sessions WHERE id > 5500") == "DELETE 500"
            await owner.execute("CREATE TABLE old_stuff (x int)")
            await owner.execute("DROP TABLE old_stuff")
        # The switch was for that transaction only.
        with pytest.raises(DENIED):
            await owner.execute("ALTER TABLE sessions DROP COLUMN note")
    finally:
        await owner.close()


async def test_a_table_made_later_is_guarded_at_once(uri):
    owner = await _as(uri, "guard_owner")
    try:
        await owner.execute("CREATE TABLE later (id int)")
        await owner.execute("INSERT INTO later SELECT generate_series(1, 5001)")
        with pytest.raises(DENIED):
            await owner.execute("TRUNCATE later")
        with pytest.raises(DENIED):
            await owner.execute("DELETE FROM later")
        assert await owner.fetchval("SELECT count(*) FROM later") == 5001
    finally:
        await owner.close()
