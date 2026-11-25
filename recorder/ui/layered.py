"""
A window whose every pixel has its own transparency (3.9.11, 26 Nov 2026).

tkinter's own see-through ("-transparentcolor") is all or nothing per pixel, so
a rounded or shadowed edge either shows a fringe of the colour it was drawn on
or is cut into a staircase - the "blurred" look of 3.9.8. Windows' layered
windows take a whole RGBA picture instead (UpdateLayeredWindow): the glass tiles
and their soft shadows blend into whatever page is behind them, exactly as an
iOS icon does. Pixels with no alpha at all let the pointer through to the page.

Plain ctypes, so no package is added. Elsewhere than Windows, or if Windows
refuses, `show` returns False and ui/overlay.py draws the strip on a plain
colour instead.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080           # not in the taskbar or Alt+Tab
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0

_ready = None


def _setup():
    """The Windows functions with their argument types, once."""
    global _ready
    if _ready is not None:
        return _ready
    if sys.platform != "win32":
        _ready = False
        return _ready
    import ctypes
    from ctypes import wintypes

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
        wintypes.HDC, ctypes.POINTER(POINT), wintypes.DWORD,
        ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                                       wintypes.DWORD]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    _ready = dict(ctypes=ctypes, user32=user32, gdi32=gdi32, POINT=POINT, SIZE=SIZE,
                  BLENDFUNCTION=BLENDFUNCTION, BITMAPINFOHEADER=BITMAPINFOHEADER)
    return _ready


def premultiplied_bgra(image) -> bytes:
    """
    The RGBA picture as Windows wants it: blue, green, red, alpha per pixel,
    each colour already multiplied by its alpha.
    """
    from PIL import Image, ImageChops

    rgba = image.convert("RGBA")
    r, g, b, a = rgba.split()
    r, g, b = (ImageChops.multiply(channel, a) for channel in (r, g, b))
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


def make_layered(hwnd: int) -> bool:
    """Turn a window into one that takes a whole RGBA picture."""
    api = _setup()
    if not api:
        return False
    try:
        user32 = api["user32"]
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TOOLWINDOW)
        return True
    except Exception as exc:
        logger.info("Could not make the control see-through: %s", exc)
        return False


def show(hwnd: int, image, x: int, y: int) -> bool:
    """Put the RGBA picture on the screen at (x, y) as the window's whole content."""
    api = _setup()
    if not api:
        return False
    ctypes, user32, gdi32 = api["ctypes"], api["user32"], api["gdi32"]
    width, height = image.size
    screen = user32.GetDC(None)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = old = None
    try:
        header = api["BITMAPINFOHEADER"]()
        header.biSize = ctypes.sizeof(header)
        header.biWidth, header.biHeight = width, -height      # top row first
        header.biPlanes, header.biBitCount, header.biCompression = 1, 32, BI_RGB
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(screen, ctypes.byref(header), DIB_RGB_COLORS,
                                        ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            return False
        data = premultiplied_bgra(image)
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(memory, bitmap)
        blend = api["BLENDFUNCTION"](AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        done = user32.UpdateLayeredWindow(
            hwnd, screen, ctypes.byref(api["POINT"](int(x), int(y))),
            ctypes.byref(api["SIZE"](width, height)), memory,
            ctypes.byref(api["POINT"](0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
        return bool(done)
    except Exception as exc:
        logger.info("Could not draw the see-through control: %s", exc)
        return False
    finally:
        if old:
            gdi32.SelectObject(memory, old)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)


__all__ = ["make_layered", "show", "premultiplied_bgra"]
