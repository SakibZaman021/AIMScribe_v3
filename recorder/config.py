"""
AIMScribe Recorder Configuration

Loaded once at startup from .env next to the executable.

Run from source (development, tests, the bench), the process environment wins
over the file, so a test or a bench script can override anything.

Installed (the frozen executable), the .env under Program Files is the only
source of AIMS_* settings: every AIMS_* variable in the environment is
discarded first. That file is writable only by an administrator, whereas any
program running as the doctor can set environment variables - and those used
to be able to switch off grant checking, point uploads at another server, or
swap the trusted public keys, all without administrator rights.

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
from typing import Dict, List, Optional

APP_NAME = "AIMScribe Recorder"
APP_VERSION = "3.7.0"

# Wire format version. The server rejects a mismatched agent rather than guessing.
PROTOCOL_VERSION = 2


# ============================================================
# Location helpers
# ============================================================

def installed_build() -> bool:
    """The packaged executable on a clinic PC, as opposed to a source run."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory holding the executable (or this file when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """
    Per-machine writable state: keys, enrolment, spool and logs. Created with
    inherited ACLs by the installer.

    `AIMS_DATA_DIR` moves the whole lot somewhere else. That is for a bench -
    a second agent on a machine that already has one enrolled, or a test rig -
    and it is the only way to run one without disturbing the other. A clinical
    PC leaves it unset and uses ProgramData.
    """
    override = os.getenv("AIMS_DATA_DIR", "").strip()
    if override:
        return Path(override)
    base = _program_data() or os.getenv("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "AIMScribe"


def _program_data() -> str:
    """
    The machine-wide data folder. Installed, it is asked of Windows itself:
    %PROGRAMDATA% is an environment variable the signed-in user can change, and
    pointing it at a folder of their own would hand the recorder their keys.
    """
    if installed_build() and sys.platform == "win32":
        try:
            import ctypes
            buffer = ctypes.create_unicode_buffer(260)
            # CSIDL_COMMON_APPDATA (0x23): the real ProgramData folder.
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x23, None, 0, buffer) == 0 \
                    and buffer.value:
                return buffer.value
        except Exception:
            pass
    return os.getenv("PROGRAMDATA", "")


def pinned_key_path(name: str) -> Path:
    """
    Where a server public key is trusted from. Installed, the installer puts
    them beside the executable, under Program Files, where the doctor's account
    cannot replace them; ProgramData is writable by every user. A source run
    keeps them with the rest of its state.
    """
    beside = app_dir() / "keys" / name
    if installed_build() and beside.is_file():
        return beside
    return data_dir() / "keys" / name


# ============================================================
# .env parsing
# ============================================================

def read_env_file(path: Path) -> Dict[str, str]:
    """The KEY=value pairs of a .env file; empty when it is missing or unreadable."""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def load_env_file(path: Path, *, installed: Optional[bool] = None) -> None:
    """
    Populate os.environ from a .env file.

    From source, existing variables are never replaced. Installed, the file is
    the only source of AIMS_* settings: every AIMS_* variable already in the
    environment is discarded first, so nothing set by the doctor's account -
    or by any program running as it - reaches the configuration.
    """
    installed = installed_build() if installed is None else installed
    values = read_env_file(path)
    if installed:
        for key in [k for k in os.environ if k.upper().startswith("AIMS_")]:
            del os.environ[key]
        os.environ.update(values)
        return
    for key, value in values.items():
        if key not in os.environ:
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
    # Part of the clinic microphone's Windows name. Opened by name and kept at
    # 100%, so nobody has to choose it in Sound settings. Empty: Windows default.
    preferred_microphone: str = "Jabra"

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
    behind it. At a minute and a half, the exposure is a minute and a half,
    transcripts start arriving while the doctor is still talking, and a poor
    connection recovers in smaller steps.

    30 to 90 seconds, cutting on a one-second gap. The window is wide on purpose:
    a longer window means more chances to find a real pause, and a cut inside a
    pause is the only cut that cannot land inside a word. A narrow window with a
    demanding gap does the opposite - it finds nothing and is forced at the
    ceiling, which is the one cut that can split a word.
    """
    min_seconds: float = 30.0
    max_seconds: float = 90.0
    # Speech rarely stops on cue. Rather than cut a talker mid-sentence at the
    # maximum, keep listening this much longer for somewhere natural to cut.
    grace_seconds: float = 30.0
    silence_rms: int = 320          # linear RMS, 0-32767; ~-40 dBFS
    silence_hold_seconds: float = 1.0
    # The clip being assembled lives in memory until it is sealed - up to a
    # minute or more of a consultation. This often it is also written, encrypted,
    # to the session folder, so a power cut or a held power button costs at most
    # this many seconds. 0 turns it off.
    partial_flush_seconds: float = 3.0


@dataclass(frozen=True)
class SpoolConfig:
    """
    Local store-and-forward buffer. Segments live here until purge-receipted.

    Sized for minutes, not weeks (SRS-SPL-06): clinic sites are networked, so
    4 GB - about 13 hours of audio - is a shock absorber. Holding a quarter of
    it means delivery has stalled (SRS-SPL-07), and a receipt deletes at once:
    the server re-hashed the stored bytes before issuing it, so waiting longer
    protects against nothing (SRS-SPL-08, SRS-REC-16).
    """
    directory: Path = field(default_factory=lambda: data_dir() / "spool")
    max_bytes: int = 4 * 1024 ** 3
    warn_ratio: float = 0.25
    critical_ratio: float = 0.9
    purge_grace_hours: int = 0

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
    # Base64 SHA-256 hashes of the server's public key (current and spare).
    # Set, any other key is refused: an antivirus re-signing HTTPS is not let in.
    tls_pins: tuple = ()

    def url(self, endpoint: str) -> str:
        return f"{self.base_url.rstrip('/')}{self.api_prefix}{endpoint}"

    @property
    def uses_tls(self) -> bool:
        return self.base_url.lower().startswith("https://")


@dataclass(frozen=True)
class SecurityConfig:
    """
    Who is allowed to drive this recorder.

    The browser is never trusted for identity. It sends CMED's five fields; the
    AIMS LAB server checks them and signs a grant, verified here against a key
    pinned at install. The clinic always comes from this PC's enrolment.
    """
    bind_host: str = "127.0.0.1"
    bind_port: int = 5050
    allowed_origins: frozenset = frozenset()
    allowed_hosts: frozenset = frozenset()
    local_api_key: str = ""
    require_grant: bool = True
    # A PC with a TPM key and no enrolment token registers itself at its first
    # CMED consultation, and is pending until CMED confirms its recordings.
    self_register: bool = True
    enable_docs: bool = False
    grant_issuer: str = "aimslab"
    grant_audience: str = "aimscribe-recorder"
    grant_public_key_path: Path = field(default_factory=lambda: pinned_key_path("aimslab_grant_pub.pem"))
    # How long `start` waits for the server's answer before replying 202
    # RECORDING_PROVISIONAL. Capture never waits (SRS-GRT-07).
    authorise_wait_seconds: float = 1.5
    # How often a recording the server said no to is asked about again. It is
    # held, never deleted: the usual cause is a fix still being made on the
    # server, and the recording is real.
    authorise_hold_retry_seconds: float = 300.0
    # A start that failed because the microphone could not be opened is tried
    # again by the recorder itself - the doctor never re-opens the patient -
    # every few seconds, for up to fifteen minutes or until CMED moves on.
    start_retry_seconds: float = 5.0
    start_retry_limit_seconds: float = 900.0
    # After a crash and restart, a consultation that was recording within this
    # long is recorded again by itself, under the same five fields.
    resume_within_seconds: float = 600.0
    # Off (0): no recording is ever ended by a clock. A consultation lasts as
    # long as it lasts - an hour's counselling, a long follow-up with no
    # prescription - and cutting it on a timer lost real consultations on the
    # bench. A recording ends when the doctor stops it, CMED starts the next
    # patient, CMED's page sends stop, or the PC shuts down. A number here
    # (seconds) brings a limit back, for a bench test only.
    counselling_limit_seconds: float = 0.0
    end_grace_seconds: float = 10.0
    no_activity_limit_seconds: float = 0.0
    receipt_public_key_path: Path = field(default_factory=lambda: pinned_key_path("aimslab_receipt_pub.pem"))
    device_key_path: Path = field(default_factory=lambda: data_dir() / "keys" / "device_ed25519.dpapi")
    allow_plaintext_keystore: bool = False
    # Where a newly enrolled PC keeps its identity key: "tpm", inside the PC's
    # security chip, where nothing can copy it (always, for an installed
    # recorder); or "software", for development and the bench. A PC enrolled
    # earlier keeps the key it enrolled with either way.
    device_key_store: str = "software"
    # Development and bench only: lets the unsafe switches (no grant check,
    # plaintext keys, the API browser) be used. Always on from source; an
    # installed recorder takes it only from its Program Files .env, so only an
    # administrator can turn it on, and it still shows as a warning.
    developer_mode: bool = True

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
        installed = installed_build()

        keys = data_dir() / "keys"

        audio = AudioConfig(
            sample_rate=_int("AIMS_SAMPLE_RATE", 44100),
            channels=_int("AIMS_CHANNELS", 1),
            sample_width=_int("AIMS_SAMPLE_WIDTH", 2),
            frames_per_buffer=_int("AIMS_FRAMES_PER_BUFFER", 2048),
            input_device_index=_opt_int("AIMS_INPUT_DEVICE_INDEX"),
            preferred_microphone=_str("AIMS_PREFERRED_MICROPHONE", "Jabra"),
        )

        segment = SegmentConfig(
            min_seconds=_float("AIMS_SEGMENT_MIN_SECONDS", 30.0),
            max_seconds=_float("AIMS_SEGMENT_MAX_SECONDS", 90.0),
            grace_seconds=_float("AIMS_SEGMENT_GRACE_SECONDS", 30.0),
            silence_rms=_int("AIMS_SILENCE_RMS", 320),
            silence_hold_seconds=_float("AIMS_SILENCE_HOLD_SECONDS", 1.0),
            partial_flush_seconds=_float("AIMS_PARTIAL_FLUSH_SECONDS", 3.0),
        )

        spool = SpoolConfig(
            directory=_path("AIMS_SPOOL_DIR", data_dir() / "spool"),
            max_bytes=_int("AIMS_SPOOL_MAX_BYTES", 4 * 1024 ** 3),
            warn_ratio=_float("AIMS_SPOOL_WARN_RATIO", 0.25),
            critical_ratio=_float("AIMS_SPOOL_CRITICAL_RATIO", 0.9),
            purge_grace_hours=_int("AIMS_PURGE_GRACE_HOURS", 0),
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
            tls_pins=tuple(_list("AIMS_TLS_PINS")),
        )

        security = SecurityConfig(
            bind_host=_str("AIMS_BIND_HOST", "127.0.0.1"),
            bind_port=_int("AIMS_BIND_PORT", 5050),
            allowed_origins=frozenset(_list("AIMS_ALLOWED_ORIGINS")),
            allowed_hosts=frozenset(h.lower() for h in _list(
                "AIMS_ALLOWED_HOSTS", "localhost:5050,127.0.0.1:5050,[::1]:5050")),
            local_api_key=_str("AIMS_LOCAL_API_KEY"),
            require_grant=_bool("AIMS_REQUIRE_GRANT", True),
            self_register=_bool("AIMS_SELF_REGISTER", True),
            enable_docs=_bool("AIMS_ENABLE_DOCS", False),
            grant_issuer=_str("AIMS_GRANT_ISSUER", "aimslab"),
            grant_audience=_str("AIMS_GRANT_AUDIENCE", "aimscribe-recorder"),
            grant_public_key_path=_path("AIMS_GRANT_PUBLIC_KEY_PATH",
                                        pinned_key_path("aimslab_grant_pub.pem")),
            receipt_public_key_path=_path("AIMS_RECEIPT_PUBLIC_KEY_PATH",
                                          pinned_key_path("aimslab_receipt_pub.pem")),
            device_key_path=keys / "device_ed25519.dpapi",
            allow_plaintext_keystore=_bool("AIMS_ALLOW_PLAINTEXT_KEYSTORE", False),
            device_key_store=("tpm" if installed
                              else _str("AIMS_DEVICE_KEY_STORE", "software").lower()),
            developer_mode=(not installed) or _bool("AIMS_DEVELOPER_MODE", False),
            counselling_limit_seconds=_float("AIMS_COUNSELLING_LIMIT_SECONDS", 0.0),
            end_grace_seconds=_float("AIMS_END_GRACE_SECONDS", 10.0),
            no_activity_limit_seconds=_float("AIMS_NO_ACTIVITY_LIMIT_SECONDS", 0.0),
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
        problems.extend(self.unsafe_switches())

        return problems

    def unsafe_switches(self) -> List[str]:
        """
        Switches that turn a protection off. Allowed only in developer mode; on
        a clinical install any one of them blocks recording outright, with the
        reason in the tray, rather than running quietly unprotected.
        """
        if self.security.developer_mode:
            return []
        found: List[str] = []
        if not self.security.require_grant:
            found.append("AIMS_REQUIRE_GRANT is false - recording without the server's "
                         "permission is for development only. Reinstall AIMScribe.")
        if self.security.allow_plaintext_keystore:
            found.append("AIMS_ALLOW_PLAINTEXT_KEYSTORE is true - private keys would sit "
                         "unprotected. Reinstall AIMScribe.")
        if self.security.enable_docs:
            found.append("AIMS_ENABLE_DOCS is true - the local control API would be "
                         "browsable. Reinstall AIMScribe.")
        if self.security.bind_host not in ("127.0.0.1", "::1", "localhost"):
            found.append(f"AIMS_BIND_HOST is {self.security.bind_host} - the recorder's "
                         "control port must listen on this PC only.")
        return found

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
        if self.security.developer_mode and installed_build():
            warnings.append("AIMS_DEVELOPER_MODE is on in an installed recorder.")
        if not self.backend.client_cert_path:
            warnings.append("No client certificate configured - mTLS is not in use.")
        return warnings

    def spool_seconds(self) -> float:
        """How long the spool can buffer at the configured audio rate."""
        return self.spool.max_bytes / max(1, self.audio.bytes_per_second)


config = Config.load()
