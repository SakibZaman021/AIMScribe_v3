"""
AIMScribe Recorder Configuration

Loaded once at startup from .env next to the executable, then from the process
environment (environment wins, so a service definition can override a file).

Deliberately dependency-free: no pydantic, no python-dotenv. The .env parser is
30 lines and keeps the packaged executable small and fast to start.

Machine state lives outside the install directory so %ProgramFiles% can stay
read-only to the logged-in user:

    %PROGRAMDATA%\\AIMScribe\\spool    encrypted segment spool
    %PROGRAMDATA%\\AIMScribe\\keys     DPAPI-wrapped device key, pinned public keys
    %PROGRAMDATA%\\AIMScribe\\logs     rotated logs, identifiers redacted
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

APP_NAME = "AIMScribe Recorder"
APP_VERSION = "2.3.1"

# Wire format version. The server rejects a mismatched agent rather than guessing.
PROTOCOL_VERSION = 2


# ============================================================
# Location helpers
# ============================================================

def app_dir() -> Path:
    """Directory holding the executable (or this file when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Per-machine writable state. Created with inherited ACLs by the installer."""
    base = os.getenv("PROGRAMDATA") or os.getenv("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "AIMScribe"


# ============================================================
# .env parsing
# ============================================================

def load_env_file(path: Path) -> None:
    """Populate os.environ from a .env file. Existing variables are never replaced."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return default if value is None or value == "" else value


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    return _str(key, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def _list(key: str, default: str = "") -> List[str]:
    return [part.strip() for part in _str(key, default).split(",") if part.strip()]


def _opt_int(key: str) -> Optional[int]:
    value = _str(key)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _path(key: str, fallback: Path) -> Path:
    value = _str(key)
    return Path(value).expanduser() if value else fallback


# ============================================================
# Configuration sections
# ============================================================

@dataclass(frozen=True)
class AudioConfig:
    """WAV PCM capture settings. These define the archive contract."""
    sample_rate: int = 44100
    channels: int = 1
    sample_width: int = 2          # 16-bit
    frames_per_buffer: int = 2048  # ~46 ms at 44.1 kHz
    input_device_index: Optional[int] = None

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width

    @property
    def bytes_per_buffer(self) -> int:
        return self.frames_per_buffer * self.channels * self.sample_width


@dataclass(frozen=True)
class SegmentConfig:
    """
    Clip boundaries. A clip closes on a quiet patch inside the window, and is
    forced once even the grace period has passed.

    Short clips, because the server merges them back into one consultation file
    anyway, so nothing is lost by cutting often - and a great deal is gained. A
    clip is the unit of upload, retry and loss: at three minutes, a failure put
    three minutes of a consultation at risk and held the rest of the session
    behind it. At a minute, the exposure is a minute, transcripts start arriving
    while the doctor is still talking, and a poor connection recovers in smaller
    steps.
    """
    min_seconds: float = 30.0
    max_seconds: float = 60.0
    # Speech rarely stops on cue. Rather than cut a talker mid-sentence at the
    # maximum, keep listening this much longer for somewhere natural to cut.
    grace_seconds: float = 15.0
    silence_rms: int = 320          # linear RMS, 0-32767; ~-40 dBFS
    silence_hold_seconds: float = 3.0


@dataclass(frozen=True)
class SpoolConfig:
    """Local store-and-forward buffer. Segments live here until purge-receipted."""
    directory: Path = field(default_factory=lambda: data_dir() / "spool")
    max_bytes: int = 40 * 1024 ** 3
    warn_ratio: float = 0.5
    critical_ratio: float = 0.8
    purge_grace_hours: int = 24

    @property
    def warn_bytes(self) -> int:
        return int(self.max_bytes * self.warn_ratio)

    @property
    def critical_bytes(self) -> int:
        return int(self.max_bytes * self.critical_ratio)


@dataclass(frozen=True)
class BackendConfig:
    """AIMS LAB server, reached over the per-PC tunnel."""
    base_url: str = "http://localhost:6000"
    # Protocol 2 routes. The v1 prefix still serves the transcription API and
    # has none of these endpoints, so pointing an agent at it fails on enrolment.
    api_prefix: str = "/api/v2"
    request_timeout: int = 30
    upload_timeout: int = 300
    retry_backoff: tuple = (2.0, 8.0, 30.0, 120.0, 600.0)
    client_cert_path: Optional[Path] = None
    client_key_path: Optional[Path] = None
    ca_bundle_path: Optional[Path] = None

    def url(self, endpoint: str) -> str:
        return f"{self.base_url.rstrip('/')}{self.api_prefix}{endpoint}"

    @property
    def uses_tls(self) -> bool:
        return self.base_url.lower().startswith("https://")


@dataclass(frozen=True)
class SecurityConfig:
    """
    Who is allowed to drive this recorder.

    The browser is never trusted for identity. Doctor, hospital and patient come
    from a CMED-signed grant; the local API key only proves the caller is the
    installed CMED front end rather than an arbitrary page.
    """
    bind_host: str = "127.0.0.1"
    bind_port: int = 5050
    allowed_origins: frozenset = frozenset()
    allowed_hosts: frozenset = frozenset()
    local_api_key: str = ""
    require_grant: bool = True
    enable_docs: bool = False
    grant_issuer: str = "cmed"
    grant_audience: str = "aimscribe-recorder"
    grant_public_key_path: Path = field(default_factory=lambda: data_dir() / "keys" / "cmed_grant_pub.pem")
    receipt_public_key_path: Path = field(default_factory=lambda: data_dir() / "keys" / "aimslab_receipt_pub.pem")
    device_key_path: Path = field(default_factory=lambda: data_dir() / "keys" / "device_ed25519.dpapi")
    allow_plaintext_keystore: bool = False

    def origin_allowed(self, origin: Optional[str]) -> bool:
        # A missing or literal "null" Origin is rejected: sandboxed iframes and
        # non-browser clients both present it, and neither should drive a recording.
        return bool(origin) and origin in self.allowed_origins

    def host_allowed(self, host: Optional[str]) -> bool:
        return bool(host) and host.lower() in self.allowed_hosts


@dataclass(frozen=True)
class PauseConfig:
    """Supervised pause. Longer pauses need a supervisor's authorisation."""
    self_authorise_seconds: int = 300
    reasons: tuple = (
        "patient_declined",
        "sensitive_personal_matter",
        "non_clinical_interruption",
        "other",
    )


@dataclass(frozen=True)
class PathConfig:
    logs_dir: Path = field(default_factory=lambda: data_dir() / "logs")
    state_dir: Path = field(default_factory=lambda: data_dir() / "state")


@dataclass(frozen=True)
class OpsConfig:
    heartbeat_seconds: int = 30
    log_level: str = "INFO"
    log_retention_days: int = 30
    redact_logs: bool = True


@dataclass(frozen=True)
class Config:
    audio: AudioConfig
    segment: SegmentConfig
    spool: SpoolConfig
    backend: BackendConfig
    security: SecurityConfig
    pause: PauseConfig
    paths: PathConfig
    ops: OpsConfig
    app_name: str = APP_NAME
    app_version: str = APP_VERSION
    protocol_version: int = PROTOCOL_VERSION

    # ---- construction ----

    @classmethod
    def load(cls) -> "Config":
        load_env_file(app_dir() / ".env")

        keys = data_dir() / "keys"

        audio = AudioConfig(
            sample_rate=_int("AIMS_SAMPLE_RATE", 44100),
            channels=_int("AIMS_CHANNELS", 1),
            sample_width=_int("AIMS_SAMPLE_WIDTH", 2),
            frames_per_buffer=_int("AIMS_FRAMES_PER_BUFFER", 2048),
            input_device_index=_opt_int("AIMS_INPUT_DEVICE_INDEX"),
        )

        segment = SegmentConfig(
            min_seconds=_float("AIMS_SEGMENT_MIN_SECONDS", 30.0),
            max_seconds=_float("AIMS_SEGMENT_MAX_SECONDS", 60.0),
            grace_seconds=_float("AIMS_SEGMENT_GRACE_SECONDS", 15.0),
            silence_rms=_int("AIMS_SILENCE_RMS", 320),
            silence_hold_seconds=_float("AIMS_SILENCE_HOLD_SECONDS", 3.0),
        )

        spool = SpoolConfig(
            directory=_path("AIMS_SPOOL_DIR", data_dir() / "spool"),
            max_bytes=_int("AIMS_SPOOL_MAX_BYTES", 40 * 1024 ** 3),
            warn_ratio=_float("AIMS_SPOOL_WARN_RATIO", 0.5),
            critical_ratio=_float("AIMS_SPOOL_CRITICAL_RATIO", 0.8),
            purge_grace_hours=_int("AIMS_PURGE_GRACE_HOURS", 24),
        )

        backoff = tuple(float(v) for v in _list("AIMS_RETRY_BACKOFF", "2,8,30,120,600"))
        backend = BackendConfig(
            base_url=_str("AIMS_BACKEND_URL", "http://localhost:6000"),
            api_prefix=_str("AIMS_BACKEND_API_PREFIX", "/api/v2"),
            request_timeout=_int("AIMS_REQUEST_TIMEOUT", 30),
            upload_timeout=_int("AIMS_UPLOAD_TIMEOUT", 300),
            retry_backoff=backoff or (2.0, 8.0, 30.0, 120.0, 600.0),
            client_cert_path=Path(_str("AIMS_CLIENT_CERT_PATH")) if _str("AIMS_CLIENT_CERT_PATH") else None,
            client_key_path=Path(_str("AIMS_CLIENT_KEY_PATH")) if _str("AIMS_CLIENT_KEY_PATH") else None,
            ca_bundle_path=Path(_str("AIMS_CA_BUNDLE_PATH")) if _str("AIMS_CA_BUNDLE_PATH") else None,
        )

        security = SecurityConfig(
            bind_host=_str("AIMS_BIND_HOST", "127.0.0.1"),
            bind_port=_int("AIMS_BIND_PORT", 5050),
            allowed_origins=frozenset(_list("AIMS_ALLOWED_ORIGINS")),
            allowed_hosts=frozenset(h.lower() for h in _list(
                "AIMS_ALLOWED_HOSTS", "localhost:5050,127.0.0.1:5050,[::1]:5050")),
            local_api_key=_str("AIMS_LOCAL_API_KEY"),
            require_grant=_bool("AIMS_REQUIRE_GRANT", True),
            enable_docs=_bool("AIMS_ENABLE_DOCS", False),
            grant_issuer=_str("AIMS_GRANT_ISSUER", "cmed"),
            grant_audience=_str("AIMS_GRANT_AUDIENCE", "aimscribe-recorder"),
            grant_public_key_path=_path("AIMS_GRANT_PUBLIC_KEY_PATH", keys / "cmed_grant_pub.pem"),
            receipt_public_key_path=_path("AIMS_RECEIPT_PUBLIC_KEY_PATH", keys / "aimslab_receipt_pub.pem"),
            device_key_path=keys / "device_ed25519.dpapi",
            allow_plaintext_keystore=_bool("AIMS_ALLOW_PLAINTEXT_KEYSTORE", False),
        )

        pause = PauseConfig(
            self_authorise_seconds=_int("AIMS_PAUSE_SELF_AUTHORISE_SECONDS", 300),
        )

        ops = OpsConfig(
            heartbeat_seconds=_int("AIMS_HEARTBEAT_SECONDS", 30),
            log_level=_str("AIMS_LOG_LEVEL", "INFO").upper(),
            log_retention_days=_int("AIMS_LOG_RETENTION_DAYS", 30),
            redact_logs=_bool("AIMS_REDACT_LOGS", True),
        )

        # Deliberately does not create directories: importing this module must
        # have no side effects on disk, or tests and tooling end up provisioning
        # %PROGRAMDATA% just by importing. main() calls ensure_directories().
        return cls(
            audio=audio, segment=segment, spool=spool, backend=backend,
            security=security, pause=pause, paths=PathConfig(), ops=ops,
        )

    # ---- runtime helpers ----

    def ensure_directories(self) -> None:
        for directory in (self.spool.directory, self.paths.logs_dir,
                          self.paths.state_dir, self.security.device_key_path.parent):
            directory.mkdir(parents=True, exist_ok=True)

    def validate(self) -> List[str]:
        """
        Return a list of problems that make this install unsafe to run.

        Called at startup: the tray reports these and refuses to accept sessions
        rather than running in a quietly insecure state.
        """
        problems: List[str] = []

        if not self.security.allowed_origins:
            problems.append(
                "AIMS_ALLOWED_ORIGINS is empty - no browser origin may start a recording.")
        if any("*" in origin for origin in self.security.allowed_origins):
            problems.append("AIMS_ALLOWED_ORIGINS must list exact origins; wildcards are not allowed.")
        if not self.security.local_api_key or self.security.local_api_key.startswith("change-me"):
            problems.append("AIMS_LOCAL_API_KEY is unset or still the placeholder value.")
        if self.security.require_grant and not self.security.grant_public_key_path.is_file():
            problems.append(
                f"Grant public key missing: {self.security.grant_public_key_path}")
        if not self.security.receipt_public_key_path.is_file():
            problems.append(
                f"Purge-receipt public key missing: {self.security.receipt_public_key_path} - "
                "local audio can never be safely deleted without it.")
        if self.segment.max_seconds <= self.segment.min_seconds:
            problems.append("AIMS_SEGMENT_MAX_SECONDS must exceed AIMS_SEGMENT_MIN_SECONDS.")
        if self.audio.sample_width != 2:
            problems.append("Only 16-bit PCM is supported (AIMS_SAMPLE_WIDTH=2).")

        return problems

    def production_warnings(self) -> List[str]:
        """Settings that are legal but must not ship to a clinical PC."""
        warnings: List[str] = []
        if not self.backend.uses_tls:
            warnings.append("Backend URL is not https - audio would cross the network in cleartext.")
        if self.security.enable_docs:
            warnings.append("AIMS_ENABLE_DOCS is true - the local API schema is browsable.")
        if not self.security.require_grant:
            warnings.append("AIMS_REQUIRE_GRANT is false - any allowed origin can start a recording.")
        if self.security.allow_plaintext_keystore:
            warnings.append("AIMS_ALLOW_PLAINTEXT_KEYSTORE is true - private keys are not DPAPI-wrapped.")
        if not self.backend.client_cert_path:
            warnings.append("No client certificate configured - mTLS is not in use.")
        return warnings

    def spool_seconds(self) -> float:
        """How long the spool can buffer at the configured audio rate."""
        return self.spool.max_bytes / max(1, self.audio.bytes_per_second)


config = Config.load()
