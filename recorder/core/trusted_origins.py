"""
The web addresses of CMED's EHR that may drive this recorder, kept by the
AIMS LAB server rather than fixed at install.

Every installer is the same, for every hospital: none is told CMED's address.
The server keeps the list (dashboard -> CMED connections) and sends it in its
heartbeat reply, signed with the key this recorder already trusts for its
purge receipts, under a domain of its own - so a signed list can never be
mistaken for a deletion receipt, nor a receipt for a list. A list is applied
only when its signature checks and its version is newer than the one held:
an old list replayed by anyone on the network is refused.

The list is kept on disk (state/trusted_origins.json), so a PC that starts
without the internet still obeys CMED's page. The addresses set at install
(AIMS_ALLOWED_ORIGINS) are still honoured as well - for a development bench.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

from cryptography.exceptions import InvalidSignature

from core.crypto import canonical_json, digest

logger = logging.getLogger(__name__)

DOMAIN = b"aimscribe.trusted-origins.v1"
FILE_NAME = "trusted_origins.json"

_lock = threading.Lock()
_state: Dict[str, Any] = {"origins": frozenset(), "version": 0, "path": None, "key": None}


def signing_input(payload: Dict[str, Any]) -> bytes:
    return digest(DOMAIN, canonical_json(payload))


def _valid_origin(text: Any) -> bool:
    if not isinstance(text, str) or "*" in text or text != text.strip() or len(text) > 200:
        return False
    scheme, sep, rest = text.partition("://")
    return sep == "://" and scheme in ("https", "http") and bool(rest) and "/" not in rest


def configure(state_dir: Optional[Path], public_key) -> None:
    """At start-up: where the list lives, the key it must be signed with, and the list held."""
    with _lock:
        _state["path"] = Path(state_dir) / FILE_NAME if state_dir else None
        _state["key"] = public_key
        _state["origins"], _state["version"] = frozenset(), 0
    path = _state["path"]
    if path is None or not path.exists():
        return
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        apply(bundle, persist=False)
    except Exception as exc:
        # A damaged file is not trusted; the next heartbeat brings the list again.
        logger.warning("Ignored the stored list of CMED addresses: %s", exc)


def apply(bundle: Any, *, persist: bool = True) -> bool:
    """
    Take a signed list from the server. True when it was newer and is now in
    force; False when it is the one already held, older, or not genuine.
    """
    if not isinstance(bundle, dict):
        return False
    payload, signature = bundle.get("payload"), bundle.get("signature")
    key = _state["key"]
    if key is None or not isinstance(payload, dict) or not isinstance(signature, str):
        return False
    if payload.get("scope") != "trusted_origins":
        return False
    try:
        key.verify(bytes.fromhex(signature), signing_input(payload))
    except (InvalidSignature, ValueError):
        logger.critical("Refused a list of CMED addresses that the server did not sign")
        return False
    version = payload.get("version")
    origins = payload.get("origins")
    if not isinstance(version, int) or not isinstance(origins, list):
        return False
    with _lock:
        if version <= _state["version"]:
            return False                          # the same list, or an older one replayed
        _state["origins"] = frozenset(o for o in origins if _valid_origin(o))
        _state["version"] = version
        path = _state["path"]
    if persist and path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(bundle), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Could not keep the list of CMED addresses on disk: %s", exc)
    logger.info("CMED addresses now trusted (version %s): %s", version,
                ", ".join(sorted(_state["origins"])) or "none")
    return True


def current() -> FrozenSet[str]:
    return _state["origins"]


def version() -> int:
    return _state["version"]


def allowed(origin: Optional[str]) -> bool:
    return bool(origin) and origin in _state["origins"]


__all__ = ["DOMAIN", "configure", "apply", "current", "version", "allowed", "signing_input"]
