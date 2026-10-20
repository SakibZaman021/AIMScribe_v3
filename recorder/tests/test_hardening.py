"""
Hardening of the doctor's PC (security review, round 3).

Each test is a hole found by the review: a program running as the doctor -
no administrator rights - could switch protections off through environment
variables, plant its own trusted keys, replay a grant across a restart, or
read patients' names from the recorder's journal. If one of these fails, that
hole is back.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import config as config_module
from core import crypto
from core.crypto import Grant, GrantError, GrantGuard
from core.spool import SessionSpool


# ============================================================
# Settings: the installed .env, not the environment
# ============================================================

def _env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_installed_the_file_wins_over_the_environment(monkeypatch, tmp_path):
    path = _env(tmp_path, "AIMS_BACKEND_URL=https://server.example\nAIMS_REQUIRE_GRANT=true\n")
    monkeypatch.setenv("AIMS_BACKEND_URL", "https://attacker.example")
    monkeypatch.setenv("AIMS_REQUIRE_GRANT", "false")
    config_module.load_env_file(path, installed=True)
    assert config_module._str("AIMS_BACKEND_URL") == "https://server.example"
    assert config_module._bool("AIMS_REQUIRE_GRANT", True) is True


def test_installed_an_aims_setting_the_file_lacks_cannot_come_from_the_environment(
        monkeypatch, tmp_path):
    """Planting a key path or a data folder the installer never wrote."""
    path = _env(tmp_path, "AIMS_BACKEND_URL=https://server.example\n")
    monkeypatch.setenv("AIMS_GRANT_PUBLIC_KEY_PATH", str(tmp_path / "mine.pem"))
    monkeypatch.setenv("AIMS_DATA_DIR", str(tmp_path / "mine"))
    monkeypatch.setenv("AIMS_ALLOW_PLAINTEXT_KEYSTORE", "true")
    config_module.load_env_file(path, installed=True)
    for key in ("AIMS_GRANT_PUBLIC_KEY_PATH", "AIMS_DATA_DIR", "AIMS_ALLOW_PLAINTEXT_KEYSTORE"):
        assert config_module._str(key) == ""


def test_from_source_the_environment_still_wins(monkeypatch, tmp_path):
    """Tests and the bench override settings by environment, as before."""
    path = _env(tmp_path, "AIMS_BACKEND_URL=https://server.example\n")
    monkeypatch.setenv("AIMS_BACKEND_URL", "http://localhost:6000")
    config_module.load_env_file(path, installed=False)
    assert config_module._str("AIMS_BACKEND_URL") == "http://localhost:6000"


def _cfg(**security):
    from config import (AudioConfig, BackendConfig, Config, OpsConfig, PathConfig,
                        PauseConfig, SecurityConfig, SegmentConfig, SpoolConfig)
    return Config(audio=AudioConfig(), segment=SegmentConfig(), spool=SpoolConfig(),
                  backend=BackendConfig(), security=SecurityConfig(**security),
                  pause=PauseConfig(), paths=PathConfig(), ops=OpsConfig())


@pytest.mark.parametrize("switch", [
    {"require_grant": False},
    {"allow_plaintext_keystore": True},
    {"enable_docs": True},
    {"bind_host": "0.0.0.0"},
])
def test_an_unsafe_switch_blocks_a_clinical_install(switch):
    cfg = _cfg(developer_mode=False, **switch)
    found = cfg.unsafe_switches()
    assert len(found) == 1
    assert found[0] in cfg.validate()          # validate() is what blocks recording


def test_developer_mode_allows_them():
    cfg = _cfg(developer_mode=True, require_grant=False, allow_plaintext_keystore=True)
    assert cfg.unsafe_switches() == []


def test_a_clinical_install_with_safe_settings_has_no_unsafe_switch():
    assert _cfg(developer_mode=False).unsafe_switches() == []


def test_pinned_keys_come_from_program_files_when_installed(monkeypatch, tmp_path):
    """ProgramData is writable by every user; Program Files is not."""
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "aimslab_grant_pub.pem").write_text("x")
    monkeypatch.setattr(config_module, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(config_module, "installed_build", lambda: True)
    assert config_module.pinned_key_path("aimslab_grant_pub.pem") == \
        tmp_path / "keys" / "aimslab_grant_pub.pem"
    monkeypatch.setattr(config_module, "installed_build", lambda: False)
    monkeypatch.setenv("AIMS_DATA_DIR", str(tmp_path / "state"))
    assert config_module.pinned_key_path("aimslab_grant_pub.pem") == \
        tmp_path / "state" / "keys" / "aimslab_grant_pub.pem"


# ============================================================
# Grants: single use survives a restart
# ============================================================

def _grant(jti="j1"):
    return Grant(jti=jti, doctor_id="DR1", doctor_name="", hospital_id="H1",
                 patient_ref="P1", consent_obtained=True, consent_method="reception",
                 expires_at=int(time.time()) + 60, raw="")


def test_a_used_grant_is_refused_after_a_restart(tmp_path):
    path = tmp_path / "used_grants.json"
    GrantGuard(path).consume(_grant())
    with pytest.raises(GrantError, match="already been used"):
        GrantGuard(path).consume(_grant())
    GrantGuard(path).consume(_grant("j2"))          # a new grant is fine


def test_an_old_entry_is_forgotten_and_a_broken_file_is_survivable(tmp_path):
    path = tmp_path / "used_grants.json"
    path.write_text(json.dumps({"used": {"old": time.time() - 7200}}))
    GrantGuard(path).consume(_grant("old"))         # past any grant's life
    path.write_text("{not json")
    GrantGuard(path).consume(_grant("j3"))          # unreadable: starts afresh
    assert "j3" in json.loads(path.read_text())["used"]


# ============================================================
# The journal names no patient
# ============================================================

def _open(spool, device_key, audio_params, patient="P0099887"):
    return spool.open_session(
        device_key=device_key, device_id="DEV1", doctor_id="DR0042",
        hospital_id="H1", patient_ref=patient, consent_method="reception",
        audio=audio_params,
        trigger={"patient_id": patient, "doctor_id": "DR0042", "hospital_id": "CMED-1",
                 "start_time": "2026-10-20T10:00:00+06:00", "date": "2026-10-20"})


def test_the_journal_on_disk_names_no_patient(spool, device_key, audio_params):
    session = _open(spool, device_key, audio_params)
    session.append_chain_entry("marker", crypto.marker_payload(
        event="patient_opened",
        patient={"patient_id": "P0055555", "doctor_id": "DR0042", "hospital_id": "CMED-1",
                 "start_time": "2026-10-20T10:05:00+06:00", "date": "2026-10-20"},
        audio_seconds=12.0, at=datetime.now(timezone.utc)))
    raw = (session.directory / "journal.jsonl").read_text(encoding="utf-8")
    assert "P0099887" not in raw and "P0055555" not in raw and "DR0042" not in raw

    recovered = SessionSpool.load(session.directory, device_key=device_key,
                                  spool_key=spool._key)
    assert recovered.meta["patient_ref"] == "P0099887"
    assert recovered.meta["trigger"]["patient_id"] == "P0099887"
    assert recovered.chain[-1].payload["patient"]["patient_id"] == "P0055555"
    assert recovered.verify_chain().ok


def test_a_sealed_line_cannot_be_moved_to_another_session(spool, device_key, audio_params):
    first = _open(spool, device_key, audio_params, patient="PA")
    second = _open(spool, device_key, audio_params, patient="PB")
    stolen = (first.directory / "journal.jsonl").read_text(encoding="utf-8").splitlines()[0]
    with open(second.directory / "journal.jsonl", "a", encoding="utf-8") as handle:
        handle.write(stolen + "\n")
    again = SessionSpool.load(second.directory, device_key=device_key, spool_key=spool._key)
    assert again.meta["patient_ref"] == "PB"


def test_a_journal_from_an_earlier_version_still_loads(spool, device_key, audio_params,
                                                       monkeypatch):
    """Upgrades: plain JSON lines written by 3.4 are read as before."""
    import core.spool as spool_mod
    monkeypatch.setattr(spool_mod, "_seal_record", lambda key, d, record: record)
    session = _open(spool, device_key, audio_params)
    assert "P0099887" in (session.directory / "journal.jsonl").read_text(encoding="utf-8")
    monkeypatch.undo()
    recovered = SessionSpool.load(session.directory, device_key=device_key,
                                  spool_key=spool._key)
    assert recovered.meta["patient_ref"] == "P0099887"
    assert recovered.verify_chain().ok


# ============================================================
# The socket
# ============================================================

async def test_a_flood_of_connections_is_refused(make_security, fake_socket):
    from api.websocket_server import MAX_CONNECTIONS, WebSocketManager
    manager = WebSocketManager(SimpleNamespace(security=make_security()))
    sockets = [fake_socket(origin="https://cmed.example") for _ in range(MAX_CONNECTIONS + 1)]
    accepted = [await manager.connect(s) for s in sockets]
    assert accepted[:MAX_CONNECTIONS] == [True] * MAX_CONNECTIONS
    assert accepted[-1] is False and sockets[-1].close_code == 4403


# ============================================================
# The installer
# ============================================================

def _installer() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "installer.iss").read_text(encoding="utf-8")


def test_the_local_key_comes_from_the_secure_generator():
    text = _installer()
    assert "BCryptGenRandom@bcrypt.dll" in text
    import re
    assert not re.search(r"[^A-Za-z]Random\(\s*Length", text)   # Pascal's clock-seeded PRNG


def test_the_trusted_keys_install_under_program_files():
    text = _installer()
    assert r'DestDir: "{app}\keys"' in text
    assert r'DestDir: "{commonappdata}\AIMScribe\keys"' not in text
    assert "AIMS_GRANT_PUBLIC_KEY_PATH=" in text


def test_the_agent_runs_without_elevation():
    text = _installer()
    assert "<RunLevel>LeastPrivilege</RunLevel>" in text
    assert "HighestAvailable" not in text
