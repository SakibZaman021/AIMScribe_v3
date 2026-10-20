"""
Security of what the archive keeps (the 2026-10 security review).

Three promises, each tested here:

  * a copy is bound to its recording - one put under another recording's
    name is refused on restore, not restored as the wrong patient, and
    copies made before binding still restore;
  * the clinical JSON, which names the patient, leaves this disk with its
    audio and is removed by overwriting first;
  * every file the worker writes is readable by its own user only (on a
    filesystem with POSIX modes - the server; Windows has no such modes).

    cd archive_worker && python -m pytest -q test_security.py
"""
from __future__ import annotations

import json
import os
import stat
import struct
import sys
from pathlib import Path

import pytest

import archive
import cloudcopy
import restore
from cloudcopy import CopyError
from test_worker_copy import KEY, SESSION, STEM, catalogued, later, setup  # noqa: F401

POSIX = os.name == "posix"


def plain_file(path: Path, text: str = "x" * 5000) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _old_format_copy(source: Path, target: Path, key: bytes) -> Path:
    """A format-1 object, as the worker made them before copies were bound."""
    aes = cloudcopy._cipher(key)
    data = source.read_bytes()
    nonce = os.urandom(cloudcopy.NONCE_BYTES)
    sealed = aes.encrypt(nonce, data, cloudcopy.MAGIC + struct.pack("<I?", 0, True))
    target.write_bytes(struct.pack("<8sI", cloudcopy.MAGIC, cloudcopy.CHUNK_BYTES)
                       + nonce + struct.pack("<I", len(sealed)) + sealed)
    return target


# ============================================================
# Copies bound to their recording
# ============================================================

def test_a_copy_names_its_recording_without_naming_the_patient(tmp_path):
    source = plain_file(tmp_path / f"{STEM}.json")
    bound = cloudcopy.binding(SESSION, "json", part_no=2, stem=STEM)
    sealed = cloudcopy.encrypt_file(source, tmp_path / f"{STEM}.v1.json.enc", KEY, bound)

    assert cloudcopy.read_binding(sealed.path) == bound
    # The header is readable by the storage provider: the patient id is not in it.
    assert b"P0012345" not in sealed.path.read_bytes()
    assert cloudcopy.sealed_sha256(sealed.path, KEY, bound) == sealed.plain_sha256


def test_a_copy_renamed_onto_another_recording_is_refused(tmp_path):
    """The swap the binding exists for: right key, wrong patient's file name."""
    source = plain_file(tmp_path / "a.wav")
    sealed = cloudcopy.encrypt_file(source, tmp_path / "a.wav.enc", KEY,
                                    cloudcopy.binding(SESSION, "audio", stem=STEM))
    other = tmp_path / "P0099999_DR0042_HOSP003_110000_111500_20260913.v1.wav.enc"
    other.write_bytes(sealed.path.read_bytes())

    with pytest.raises(CopyError, match="not the copy asked for"):
        restore.restore_one(other, tmp_path / "out", KEY)
    assert not any((tmp_path / "out").glob("*.wav"))


def test_a_json_copy_cannot_pass_for_the_audio(tmp_path):
    source = plain_file(tmp_path / "doc.json")
    sealed = cloudcopy.encrypt_file(source, tmp_path / "x.enc", KEY,
                                    cloudcopy.binding(SESSION, "json", stem=STEM))
    renamed = tmp_path / f"{STEM}.v1.wav.enc"
    renamed.write_bytes(sealed.path.read_bytes())
    with pytest.raises(CopyError, match="kind"):
        restore.restore_one(renamed, tmp_path / "out", KEY)


def test_restore_can_require_the_session(tmp_path):
    source = plain_file(tmp_path / "a.wav")
    sealed = cloudcopy.encrypt_file(source, tmp_path / f"{STEM}.v1.wav.enc", KEY,
                                    cloudcopy.binding(SESSION, "audio", stem=STEM))
    back = restore.restore_one(sealed.path, tmp_path / "ok", KEY, session_id=SESSION)
    assert back.read_bytes() == source.read_bytes()
    with pytest.raises(CopyError, match="session_id"):
        restore.restore_one(sealed.path, tmp_path / "no", KEY,
                            session_id="01JB8XQ4M7YZ2K9V3N5P6R8T0X")


def test_editing_the_binding_breaks_the_copy(tmp_path):
    """The binding is sealed into every chunk: changing it fails decryption."""
    source = plain_file(tmp_path / "a.wav")
    sealed = cloudcopy.encrypt_file(source, tmp_path / "a.wav.enc", KEY,
                                    cloudcopy.binding(SESSION, "audio", stem=STEM))
    data = sealed.path.read_bytes()
    forged = data.replace(SESSION.encode(), b"01JB8XQ4M7YZ2K9V3N5P6R8T0X")
    assert forged != data
    sealed.path.write_bytes(forged)
    with pytest.raises(CopyError, match="does not decrypt"):
        cloudcopy.decrypt_file(sealed.path, tmp_path / "back.wav", KEY)


def test_a_copy_made_before_binding_still_restores(tmp_path):
    source = plain_file(tmp_path / "old.wav")
    old = _old_format_copy(source, tmp_path / f"{STEM}.v1.wav.enc", KEY)
    assert cloudcopy.read_binding(old) is None
    back = restore.restore_one(old, tmp_path / "out", KEY)
    assert back.read_bytes() == source.read_bytes()


def test_the_worker_binds_every_copy_it_uploads(setup, tmp_path):
    worker, backend, audio = setup
    assert worker.copy_pass() == 1
    for kind in ("audio", "json"):
        blob = tmp_path / f"up.{kind}.enc"
        blob.write_bytes(backend.stored(kind))
        found = cloudcopy.read_binding(blob)
        assert found["session_id"] == SESSION and found["kind"] == kind
        assert found["stem_sha256"] == cloudcopy.stem_fingerprint(STEM)


# ============================================================
# The clinical JSON leaves with its audio
# ============================================================

def test_the_json_leaves_with_the_wav_and_the_manifest_says_so(setup):
    worker, backend, audio = setup
    catalogued(worker, audio)
    worker.copy_pass()
    assert worker.release_pass(now=later(8)) == 1
    assert not audio.exists() and not audio.with_suffix(".json").exists()
    manifest = json.loads(audio.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert "clinical_json" in manifest["local_audio"]


def test_a_json_written_again_before_the_copy_key_existed_is_kept(setup):
    """No copy uploaded means this file is the only one: it stays."""
    worker, backend, audio = setup
    audio.unlink()
    archive.record_local_release(audio.with_suffix(".manifest.json"),
                                 released_at="2026-10-01T00:00:00+00:00",
                                 copy_object_key="k", sha256="0" * 64)
    worker.drop_json_if_released(audio, [])
    assert audio.with_suffix(".json").exists()
    worker.drop_json_if_released(audio, [{"kind": "json"}])
    assert not audio.with_suffix(".json").exists()


def test_removal_overwrites_before_deleting(tmp_path, monkeypatch):
    target = plain_file(tmp_path / "record.json", "Ayesha Rahman " * 50)
    seen = {}
    real_unlink = Path.unlink

    def spy(self, missing_ok=False):
        if self == target:
            seen["bytes"] = self.read_bytes()
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", spy)
    assert archive.remove_securely(target) is True
    assert seen["bytes"] == b"\0" * len(seen["bytes"]) and b"Ayesha" not in seen["bytes"]
    assert not target.exists()
    assert archive.remove_securely(target) is False


# ============================================================
# Owner-only files
# ============================================================

def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(not POSIX, reason="POSIX file modes (the server is Linux)")
def test_everything_written_is_owner_only(setup, tmp_path):
    worker, backend, audio = setup
    folder = archive.make_dirs(tmp_path / "H" / "D" / "2026-10-01" / "S")
    for level in (tmp_path / "H", tmp_path / "H" / "D", folder):
        assert _mode(level) == 0o700
    doc = archive.write_clinical_json(folder / "S.wav", {"patient": "x"})
    index = archive.update_day_index(folder)
    assert _mode(doc) == 0o600 and _mode(index) == 0o600

    restored = cloudcopy.decrypt_file(
        cloudcopy.encrypt_file(doc, tmp_path / "d.enc", KEY).path, tmp_path / "back", KEY)
    assert _mode(restored) == 0o600


@pytest.mark.skipif(not POSIX, reason="POSIX file modes (the server is Linux)")
def test_an_existing_archive_is_tightened_once(tmp_path):
    loose = tmp_path / "H" / "old.json"
    loose.parent.mkdir()
    loose.write_text("{}")
    os.chmod(loose, 0o644)
    os.chmod(loose.parent, 0o755)
    assert archive.tighten_tree(tmp_path) >= 2
    assert _mode(loose) == 0o600 and _mode(loose.parent) == 0o700
    assert archive.tighten_tree(tmp_path) == 0                 # once per volume


def test_tightening_runs_everywhere_without_failing(tmp_path):
    """On Windows chmod only toggles read-only; the pass must still complete."""
    plain_file(tmp_path / "a.json")
    assert archive.tighten_tree(tmp_path) >= 1
    assert (tmp_path / ".permissions_v1").exists()
    assert (tmp_path / "a.json").read_text(encoding="utf-8")


def test_a_worker_that_does_not_check_the_certificate_says_so(monkeypatch):
    from worker import Settings
    monkeypatch.setenv("AIMS_BACKEND_URL", "https://aims.example.org")
    monkeypatch.setenv("AIMS_WORKER_KEY", "k")
    monkeypatch.setenv("AIMS_VERIFY_TLS", "false")
    assert any("AIMS_VERIFY_TLS" in p for p in Settings().problems())
    monkeypatch.setenv("AIMS_BACKEND_URL", "http://api:6000")       # inside the machine
    assert not any("AIMS_VERIFY_TLS" in p for p in Settings().problems())
