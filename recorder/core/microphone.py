"""
The clinic microphone, made ready without anyone opening Sound settings.

Each consultation PC has a Jabra speakerphone. Windows may not make it the
default, may leave its level at 60%, and someone may mute it. The agent does
not depend on any of that: it opens the Jabra by name (see core.recorder), and
this module keeps that microphone's Windows level at 100% and unmuted, checked
at start-up and on every idle heartbeat.

Plain ctypes over the Windows Core Audio interfaces, so no package is added to
the executable. Only the endpoint's own level and mute are touched; the default
device and every other setting are left as they are.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# The name the clinic microphone shows in Windows, matched case-insensitively.
DEFAULT_PREFERRED = "Jabra"

E_CAPTURE = 1
DEVICE_STATE_ACTIVE = 1
CLSCTX_ALL = 23
STGM_READ = 0
VT_LPWSTR = 31


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


def _call(obj, index: int, *args, argtypes=()):
    """Call method `index` of a COM interface pointer. Raises OSError on failure."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])
    return fn(obj, *args)


def _release(obj) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


def _endpoints(apply_level: Optional[float], match: str) -> List[Tuple[str, float, bool]]:
    """
    Every active recording device as (name, level 0-1, muted). Where the name
    contains `match` and `apply_level` is given, that level is set and mute
    cleared first; the values returned are as they are afterwards.
    """
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, 0)          # COINIT_MULTITHREADED
    uninit = hr in (0, 1)                       # S_OK / S_FALSE: ours to undo
    found: List[Tuple[str, float, bool]] = []
    enum = ctypes.c_void_p()
    coll = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(CLSID_ENUMERATOR), None, CLSCTX_ALL,
                               ctypes.byref(IID_ENUMERATOR), ctypes.byref(enum))
        if not enum:
            return found
        _call(enum, 3, E_CAPTURE, DEVICE_STATE_ACTIVE, ctypes.byref(coll),
              argtypes=(ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)))
        count = ctypes.c_uint()
        _call(coll, 3, ctypes.byref(count), argtypes=(ctypes.POINTER(ctypes.c_uint),))
        for i in range(count.value):
            device, store, volume = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            try:
                _call(coll, 4, i, ctypes.byref(device),
                      argtypes=(ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)))
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
                if apply_level is not None and match.lower() in name.lower():
                    _call(volume, 7, ctypes.c_float(apply_level), None,
                          argtypes=(ctypes.c_float, ctypes.c_void_p))
                    _call(volume, 14, 0, None, argtypes=(ctypes.c_int, ctypes.c_void_p))
                level, muted = ctypes.c_float(), ctypes.c_int()
                _call(volume, 9, ctypes.byref(level), argtypes=(ctypes.POINTER(ctypes.c_float),))
                _call(volume, 15, ctypes.byref(muted), argtypes=(ctypes.POINTER(ctypes.c_int),))
                found.append((name, round(level.value, 3), bool(muted.value)))
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


def recording_devices() -> List[Tuple[str, float, bool]]:
    """What Windows has plugged in, for diagnostics. Changes nothing."""
    if sys.platform != "win32":
        return []
    try:
        return _endpoints(None, "")
    except Exception as exc:
        logger.warning("Could not list recording devices: %s", exc)
        return []


def make_ready(preferred: str = DEFAULT_PREFERRED, level: float = 1.0) -> Optional[str]:
    """
    Set the preferred microphone to `level` (100%) and unmute it. Returns its
    name, or None when it is not plugged in - recording then uses the Windows
    default, which is the PC's own microphone.
    """
    if sys.platform != "win32" or not preferred:
        return None
    try:
        for name, now, muted in _endpoints(level, preferred):
            if preferred.lower() in name.lower():
                if now < level - 0.01 or muted:
                    logger.warning("%s is at %.0f%%%s after setting it", name, now * 100,
                                   ", muted" if muted else "")
                return name
    except Exception as exc:
        logger.warning("Could not prepare the microphone: %s", exc)
    return None


__all__ = ["DEFAULT_PREFERRED", "make_ready", "recording_devices"]
