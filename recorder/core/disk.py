"""
The drive the spool lives on: how full it is, and whether it is encrypted.

Free space is checked where audio is written, not only against the spool's own
cap: a laptop whose C: drive is full of somebody's downloads cannot hold the
next clip however empty the spool is.

BitLocker is asked once, at start-up, on its own thread and without
administrator rights - the Shell's property system exposes a drive's
protection state to any user. It is read in-process (ctypes, no PowerShell, no
child process): a recorder that starts hidden PowerShell is exactly what
antivirus behaviour rules flag. A stolen laptop whose drive is not encrypted gives up its audio to anyone
who can sign in to it; the dashboard shows which PCs those are.
"""
from __future__ import annotations

import ctypes
import logging
import shutil
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Below this a new recording is refused (AGENT_NOT_READY) and disk_full raised:
# an hour of WAV is about 320 MB, so this is two or three consultations of room.
MIN_FREE_BYTES = 1 << 30


def free_bytes(path: Path) -> Optional[int]:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _probe_bitlocker(drive: str) -> Optional[bool]:
    """1 protected, 0 or 2 not; anything else (or no answer) is unknown."""
    if sys.platform != "win32" or not drive:
        return None
    try:
        value = _volume_property(drive.rstrip("\\") + "\\",
                                 "System.Volume.BitLockerProtection")
    except Exception as exc:
        logger.debug("BitLocker probe failed: %s", exc)
        return None
    if value == 1:
        return True
    if value in (0, 2):
        return False
    return None


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("r1", ctypes.c_uint16), ("r2", ctypes.c_uint16),
                ("r3", ctypes.c_uint16), ("value", ctypes.c_int64), ("pad", ctypes.c_int64)]


# IID_IPropertyStore {886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}
_IID_IPROPERTYSTORE = _GUID(0x886D8EEB, 0x8CF2, 0x4446,
                            (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99))
_VT_I4, _VT_UI4, _VT_I2, _VT_UI2, _VT_UI1, _VT_INT, _VT_UINT = 3, 19, 2, 18, 17, 22, 23


def _volume_property(path: str, name: str) -> Optional[int]:
    """An integer Shell property of a drive (e.g. 'C:\'), read through IPropertyStore."""
    ole32, shell32, propsys = ctypes.OleDLL("ole32"), ctypes.OleDLL("shell32"),         ctypes.OleDLL("propsys")
    initialised = ole32.CoInitializeEx(None, 0x2) in (0, 1)       # COINIT_APARTMENTTHREADED
    store = ctypes.c_void_p()
    try:
        key = _PROPERTYKEY()
        propsys.PSGetPropertyKeyFromName(ctypes.c_wchar_p(name), ctypes.byref(key))
        shell32.SHGetPropertyStoreFromParsingName(
            ctypes.c_wchar_p(path), None, 0, ctypes.byref(_IID_IPROPERTYSTORE),
            ctypes.byref(store))
        vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0],
                             ctypes.POINTER(ctypes.c_void_p))
        get_value = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p,
                                       ctypes.POINTER(_PROPERTYKEY),
                                       ctypes.POINTER(_PROPVARIANT))(vtable[5])
        variant = _PROPVARIANT()
        get_value(store, ctypes.byref(key), ctypes.byref(variant))
        try:
            if variant.vt in (_VT_I4, _VT_INT, _VT_I2):
                return ctypes.c_int32(variant.value & 0xFFFFFFFF).value
            if variant.vt in (_VT_UI4, _VT_UINT, _VT_UI2, _VT_UI1):
                return int(variant.value & 0xFFFFFFFF)
            return None
        finally:
            ole32.PropVariantClear(ctypes.byref(variant))
    finally:
        if store:
            vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0],
                                 ctypes.POINTER(ctypes.c_void_p))
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(store)
        if initialised:
            ole32.CoUninitialize()


class DriveFacts:
    """Measured in the background, read by the heartbeat. None until known."""

    def __init__(self, spool_root: Path):
        self.root = Path(spool_root)
        self.encrypted: Optional[bool] = None
        self.probed = False

    def probe_in_background(self) -> threading.Thread:
        drive = (self.root.resolve().drive or "").rstrip("\\")

        def run() -> None:
            self.encrypted = _probe_bitlocker(drive)
            self.probed = True
            if self.encrypted is False:
                logger.warning("The drive holding the audio (%s) is not encrypted with "
                               "BitLocker", drive)

        thread = threading.Thread(target=run, name="DriveProbe", daemon=True)
        thread.start()
        return thread

    def free_bytes(self) -> Optional[int]:
        return free_bytes(self.root)


__all__ = ["DriveFacts", "MIN_FREE_BYTES", "free_bytes"]
