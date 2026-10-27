"""
The clinic microphone, made ready without anyone opening Sound settings.

Each consultation PC has a Jabra Speak2 55. Windows names it "Echo Cancelling
Speakerphone (Jabra Speak2 55 MS)", may not make it the default, may leave its
level at 60%, and someone may mute it. This module, at start-up, before every
recording and on every idle heartbeat:

  * makes the Jabra the Windows default recording device, for every role
    (console, multimedia, communications), so any program records from it;
  * sets its level to 100% and unmutes it;
  * gives core.recorder the full names of the recording devices, because the
    audio library sees them cut to 31 characters ("Echo Cancelling
    Speakerphone (J"), which no longer contain "Jabra".

When the Jabra is not plugged in, nothing is changed and the recording uses
the Windows default - the PC's own microphone - so a consultation is never
lost to a missing speakerphone.

Plain ctypes over the Windows Core Audio interfaces, so no package is added to
the executable. Making a device the default uses IPolicyConfig, the interface
Windows' own Sound settings use; it is not in the public SDK but has been
stable since Windows 7.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# The name the clinic microphone shows in Windows, matched case-insensitively.
DEFAULT_PREFERRED = "Jabra"

E_CAPTURE = 1
DEVICE_STATE_ACTIVE = 1
CLSCTX_ALL = 23
STGM_READ = 0
VT_LPWSTR = 31
ROLES = (0, 1, 2)                     # eConsole, eMultimedia, eCommunications


class GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text: str) -> "GUID":
        g = cls()
        ctypes.memmove(g.data, uuid.UUID(text).bytes_le, 16)
        return g


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


CLSID_ENUMERATOR = GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_ENUMERATOR = GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_ENDPOINT_VOLUME = GUID.of("5CDF2C82-841E-4546-9722-0CF74078229A")
PKEY_FRIENDLY_NAME = PROPERTYKEY(GUID.of("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)
CLSID_POLICY_CONFIG = GUID.of("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")
IID_POLICY_CONFIG = GUID.of("F8679F50-850A-41CF-9C72-430F290290C8")


@dataclass
class Endpoint:
    """One active recording device, as Windows sees it."""
    name: str
    endpoint_id: str
    level: float
    muted: bool
    default: bool


def _call(obj, index: int, *args, argtypes=()):
    """Call method `index` of a COM interface pointer. Raises OSError on failure."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])
    return fn(obj, *args)


def _release(obj) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


def _device_id(device) -> str:
    """IMMDevice::GetId."""
    out = ctypes.c_wchar_p()
    _call(device, 5, ctypes.byref(out), argtypes=(ctypes.POINTER(ctypes.c_wchar_p),))
    try:
        return out.value or ""
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def _default_id(enum) -> str:
    """The id of the Windows default recording device (console role), or ''."""
    device = ctypes.c_void_p()
    try:
        _call(enum, 4, E_CAPTURE, 0, ctypes.byref(device),
              argtypes=(ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)))
        return _device_id(device) if device else ""
    except OSError:
        return ""                               # no recording device at all
    finally:
        _release(device)


def _endpoints(apply_level: Optional[float], match: str) -> List[Endpoint]:
    """
    Every active recording device. Where the name contains `match` and
    `apply_level` is given, that level is set and mute cleared first; the
    values returned are as they are afterwards.
    """
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, 0)          # COINIT_MULTITHREADED
    uninit = hr in (0, 1)                       # S_OK / S_FALSE: ours to undo
    found: List[Endpoint] = []
    enum = ctypes.c_void_p()
    coll = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(CLSID_ENUMERATOR), None, CLSCTX_ALL,
                               ctypes.byref(IID_ENUMERATOR), ctypes.byref(enum))
        if not enum:
            return found
        default = _default_id(enum)
        _call(enum, 3, E_CAPTURE, DEVICE_STATE_ACTIVE, ctypes.byref(coll),
              argtypes=(ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)))
        count = ctypes.c_uint()
        _call(coll, 3, ctypes.byref(count), argtypes=(ctypes.POINTER(ctypes.c_uint),))
        for i in range(count.value):
            device, store, volume = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            try:
                _call(coll, 4, i, ctypes.byref(device),
                      argtypes=(ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)))
                endpoint_id = _device_id(device)
                _call(device, 4, STGM_READ, ctypes.byref(store),
                      argtypes=(ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)))
                value = PROPVARIANT()
                _call(store, 5, ctypes.byref(PKEY_FRIENDLY_NAME), ctypes.byref(value),
                      argtypes=(ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)))
                name = ctypes.wstring_at(value.ptr) if value.vt == VT_LPWSTR and value.ptr \
                    else "microphone"
                ole32.PropVariantClear(ctypes.byref(value))

                _call(device, 3, ctypes.byref(IID_ENDPOINT_VOLUME), CLSCTX_ALL, None,
                      ctypes.byref(volume),
                      argtypes=(ctypes.POINTER(GUID), ctypes.c_ulong, ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_void_p)))
                if apply_level is not None and match and match.lower() in name.lower():
                    _call(volume, 7, ctypes.c_float(apply_level), None,
                          argtypes=(ctypes.c_float, ctypes.c_void_p))
                    _call(volume, 14, 0, None, argtypes=(ctypes.c_int, ctypes.c_void_p))
                level, muted = ctypes.c_float(), ctypes.c_int()
                _call(volume, 9, ctypes.byref(level), argtypes=(ctypes.POINTER(ctypes.c_float),))
                _call(volume, 15, ctypes.byref(muted), argtypes=(ctypes.POINTER(ctypes.c_int),))
                found.append(Endpoint(name, endpoint_id, round(level.value, 3),
                                      bool(muted.value), endpoint_id == default))
            except OSError as exc:
                logger.debug("Skipped recording device %s: %s", i, exc)
            finally:
                _release(volume)
                _release(store)
                _release(device)
    finally:
        _release(coll)
        _release(enum)
        if uninit:
            ole32.CoUninitialize()
    return found


def _set_default(endpoint_id: str) -> bool:
    """Make one recording device the Windows default for every role."""
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, 0)
    uninit = hr in (0, 1)
    policy = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(CLSID_POLICY_CONFIG), None, CLSCTX_ALL,
                               ctypes.byref(IID_POLICY_CONFIG), ctypes.byref(policy))
        if not policy:
            return False
        for role in ROLES:
            # IPolicyConfig::SetDefaultEndpoint(LPCWSTR id, ERole role)
            _call(policy, 13, endpoint_id, role, argtypes=(ctypes.c_wchar_p, ctypes.c_int))
        return True
    except OSError as exc:
        logger.warning("Could not make the clinic microphone the Windows default: %s", exc)
        return False
    finally:
        _release(policy)
        if uninit:
            ole32.CoUninitialize()


def recording_devices() -> List[Tuple[str, float, bool]]:
    """What Windows has plugged in, for diagnostics. Changes nothing."""
    if sys.platform != "win32":
        return []
    try:
        return [(e.name, e.level, e.muted) for e in _endpoints(None, "")]
    except Exception as exc:
        logger.warning("Could not list recording devices: %s", exc)
        return []


def endpoint_names() -> List[str]:
    """The full names of the active recording devices (none off Windows)."""
    if sys.platform != "win32":
        return []
    try:
        return [e.name for e in _endpoints(None, "")]
    except Exception as exc:
        logger.debug("Could not list recording devices: %s", exc)
        return []


def same_device(short: str, full: str) -> bool:
    """
    Whether the audio library's name for a device (cut to 31 characters, as
    the Windows MME interface does) is this full Windows name.
    """
    short, full = short.strip().lower(), full.strip().lower()
    return bool(short) and (short == full or (len(short) >= 12 and full.startswith(short)))


def make_ready(preferred: str = DEFAULT_PREFERRED, level: float = 1.0) -> Optional[str]:
    """
    Make the preferred microphone the Windows default, at `level` (100%) and
    unmuted. Returns its full name, or None when it is not plugged in -
    recording then uses the Windows default, the PC's own microphone.
    """
    if sys.platform != "win32" or not preferred:
        return None
    try:
        for e in _endpoints(level, preferred):
            if preferred.lower() not in e.name.lower():
                continue
            if e.level < level - 0.01 or e.muted:
                logger.warning("%s is at %.0f%%%s after setting it", e.name, e.level * 100,
                               ", muted" if e.muted else "")
            if not e.default and _set_default(e.endpoint_id):
                logger.info("%s is now the Windows default microphone", e.name)
            return e.name
    except Exception as exc:
        logger.warning("Could not prepare the microphone: %s", exc)
    return None


def current() -> Optional[Endpoint]:
    """The Windows default recording device now, for the heartbeat and the dashboard."""
    if sys.platform != "win32":
        return None
    try:
        return next((e for e in _endpoints(None, "") if e.default), None)
    except Exception as exc:
        logger.debug("Could not read the default microphone: %s", exc)
        return None


__all__ = ["DEFAULT_PREFERRED", "Endpoint", "make_ready", "recording_devices",
           "endpoint_names", "same_device", "current"]
