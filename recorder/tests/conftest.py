"""Shared fixtures. Everything runs against a temp directory; nothing touches %PROGRAMDATA%."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.crypto import DeviceKey  # noqa: E402
from core.spool import Spool  # noqa: E402


@pytest.fixture
def device_key(tmp_path: Path) -> DeviceKey:
    return DeviceKey.load_or_create(tmp_path / "device.dpapi", allow_plaintext=True)


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    return Spool(
        tmp_path / "spool",
        key_path=tmp_path / "spool.key",
        allow_plaintext=True,
        max_bytes=64 * 1024 * 1024,
        warn_bytes=32 * 1024 * 1024,
        critical_bytes=48 * 1024 * 1024,
    )


@pytest.fixture
def audio_params() -> dict:
    """The production audio contract: WAV PCM 44.1 kHz / 16-bit / mono."""
    return {"sample_rate": 44100, "channels": 1, "sample_width": 2}


def pcm(seconds: float, *, sample_rate: int = 44100, value: int = 0) -> bytes:
    """Silence, or a constant level, as 16-bit little-endian mono PCM."""
    frames = int(seconds * sample_rate)
    return value.to_bytes(2, "little", signed=True) * frames


@pytest.fixture
def make_security():
    """Build a SecurityConfig with test-friendly defaults."""
    from config import SecurityConfig

    def _make(**overrides):
        defaults = dict(
            bind_host="127.0.0.1",
            bind_port=5050,
            allowed_origins=frozenset({"https://cmed.example"}),
            allowed_hosts=frozenset({"localhost:5050", "127.0.0.1:5050"}),
            local_api_key="test-key",
            require_grant=True,
            enable_docs=False,
        )
        defaults.update(overrides)
        return SecurityConfig(**defaults)

    return _make


class FakeWebSocket:
    """Minimal stand-in for starlette's WebSocket, enough for the handshake checks."""

    def __init__(self, *, origin=None, host="localhost:5050", peer="127.0.0.1"):
        headers = {}
        if origin is not None:
            headers["origin"] = origin
        if host is not None:
            headers["host"] = host
        self.headers = headers
        self.client = SimpleNamespace(host=peer) if peer else None
        self.accepted = False
        self.close_code = None
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.close_code = code

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.fixture
def fake_socket():
    return FakeWebSocket
