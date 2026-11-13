"""Linux on X11: pictures with mss (pointer included), input with the XTest extension (libXtst, part of every X11
desktop). Input uses its own X connection, opened on Cloudora's input thread."""

from __future__ import annotations

import ctypes
import ctypes.util

from cloudora.desktop import DesktopUnavailable, WheelAccumulator
from cloudora.desktop.keys import Key
from cloudora.desktop.mss_capture import MssBackend

_BUTTONS = {0: 1, 1: 2, 2: 3}      # browser left/middle/right -> X buttons 1/2/3


class X11Backend(MssBackend):
    name = "X11"
    cursor_in_frames = True
    host_repeats = True
    with_cursor = True

    def __init__(self) -> None:
        super().__init__()
        x11, xtst = ctypes.util.find_library("X11"), ctypes.util.find_library("Xtst")
        if not x11 or not xtst:
            raise DesktopUnavailable("The X11 input library is missing on the computer (install libXtst: "
                                     "sudo apt install libxtst6 / sudo dnf install libXtst).")
        self.X = ctypes.cdll.LoadLibrary(x11)
        self.T = ctypes.cdll.LoadLibrary(xtst)
        X, T, vp, ul = self.X, self.T, ctypes.c_void_p, ctypes.c_ulong
        X.XOpenDisplay.argtypes, X.XOpenDisplay.restype = [ctypes.c_char_p], vp
        X.XFlush.argtypes = X.XSync.argtypes = [vp]
        X.XSync.argtypes = [vp, ctypes.c_int]
        X.XCloseDisplay.argtypes = [vp]
        T.XTestFakeMotionEvent.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ul]
        T.XTestFakeRelativeMotionEvent.argtypes = [vp, ctypes.c_int, ctypes.c_int, ul]
        T.XTestFakeButtonEvent.argtypes = [vp, ctypes.c_uint, ctypes.c_int, ul]
        T.XTestFakeKeyEvent.argtypes = [vp, ctypes.c_uint, ctypes.c_int, ul]
        self._dpy = None
        self._wheel = WheelAccumulator()

    def _display(self):
        if self._dpy is None:
            self._dpy = self.X.XOpenDisplay(None)
            if not self._dpy:
                raise DesktopUnavailable("Cannot open the X display for input.")
        return self._dpy

    def move(self, screen: int, fx: float, fy: float) -> None:
        m = self.monitor(screen)
        x = m["left"] + round(fx * (m["width"] - 1))
        y = m["top"] + round(fy * (m["height"] - 1))
        d = self._display()
        self.T.XTestFakeMotionEvent(d, -1, x, y, 0)
        self.X.XFlush(d)

    def nudge(self, dx: int, dy: int) -> None:
        d = self._display()
        self.T.XTestFakeRelativeMotionEvent(d, int(dx), int(dy), 0)
        self.X.XFlush(d)

    def button(self, button: int, down: bool) -> None:
        d = self._display()
        self.T.XTestFakeButtonEvent(d, _BUTTONS[button], 1 if down else 0, 0)
        self.X.XFlush(d)

    def wheel(self, dx: int, dy: int) -> None:
        sx, sy = self._wheel.add(dx, dy)
        d = self._display()
        for steps, neg, pos in ((sy, 4, 5), (sx, 6, 7)):
            for _ in range(min(abs(steps), 20)):
                b = pos if steps > 0 else neg
                self.T.XTestFakeButtonEvent(d, b, 1, 0)
                self.T.XTestFakeButtonEvent(d, b, 0, 0)
        self.X.XFlush(d)

    def key(self, key: Key, down: bool) -> None:
        d = self._display()
        self.T.XTestFakeKeyEvent(d, key.evdev + 8, 1 if down else 0, 0)
        self.X.XFlush(d)

    def close(self) -> None:
        super().close()
        dpy, self._dpy = self._dpy, None
        if dpy:
            try:
                self.X.XCloseDisplay(dpy)
            except Exception:  # noqa: BLE001
                pass
