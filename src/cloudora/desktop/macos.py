"""macOS: pictures with mss (Quartz), input with Quartz events.

macOS asks once for two permissions for the app Cloudora runs in (Terminal, iTerm, ...): **Screen Recording** (to see
the screen) and **Accessibility** (to control it). Cloudora checks both and says exactly what to allow in System
Settings > Privacy & Security. Positions are in points, so Retina scaling needs no special handling.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import time

from cloudora.desktop import DesktopUnavailable
from cloudora.desktop.keys import Key
from cloudora.desktop.mss_capture import MssBackend


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


# Quartz event types and buttons
MOVED = 5
DOWN = {0: 1, 2: 3, 1: 25}        # browser button -> kCGEventLeftMouseDown / RightMouseDown / OtherMouseDown
UP = {0: 2, 2: 4, 1: 26}
DRAG = {0: 6, 2: 7, 1: 27}
CG_BUTTON = {0: 0, 2: 1, 1: 2}    # kCGMouseButtonLeft / Right / Center
CLICK_STATE = 1                   # kCGMouseEventClickState
HID_TAP = 0                       # kCGHIDEventTap
FLAG = {0x38: 0x20000, 0x3C: 0x20000, 0x3B: 0x40000, 0x3E: 0x40000, 0x3A: 0x80000, 0x3D: 0x80000,
        0x37: 0x100000, 0x36: 0x100000}   # modifier keycode -> event flag (shift, control, option, command)


def _framework(name: str):
    path = ctypes.util.find_library(name) or f"/System/Library/Frameworks/{name}.framework/{name}"
    return ctypes.cdll.LoadLibrary(path)


class MacBackend(MssBackend):
    name = "macOS"
    cursor_in_frames = False

    def __init__(self) -> None:
        super().__init__()
        try:
            cg = self.cg = _framework("CoreGraphics")
            cf = self.cf = _framework("CoreFoundation")
            hi = _framework("ApplicationServices")
        except OSError as exc:
            raise DesktopUnavailable(f"Cannot load macOS graphics libraries: {exc}") from exc
        vp = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [vp, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
        cg.CGEventCreateMouseEvent.restype = vp
        cg.CGEventCreateKeyboardEvent.argtypes = [vp, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateKeyboardEvent.restype = vp
        cg.CGEventCreateScrollWheelEvent2.argtypes = [vp, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
                                                      ctypes.c_int32, ctypes.c_int32]
        cg.CGEventCreateScrollWheelEvent2.restype = vp
        cg.CGEventSetFlags.argtypes = [vp, ctypes.c_uint64]
        cg.CGEventSetIntegerValueField.argtypes = [vp, ctypes.c_uint32, ctypes.c_int64]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, vp]
        cg.CGEventCreate.argtypes, cg.CGEventCreate.restype = [vp], vp
        cg.CGEventGetLocation.argtypes, cg.CGEventGetLocation.restype = [vp], CGPoint
        cf.CFRelease.argtypes = [vp]
        self.input_ok = True
        try:
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            if not cg.CGPreflightScreenCaptureAccess():
                cg.CGRequestScreenCaptureAccess()
                raise DesktopUnavailable("macOS has not allowed Cloudora to see the screen. On the Mac: System Settings "
                                         "> Privacy & Security > Screen Recording, turn on the app Cloudora runs in "
                                         "(e.g. Terminal), then restart Cloudora.")
        except AttributeError:
            pass   # macOS before 10.15 has no such permission
        try:
            hi.AXIsProcessTrusted.restype = ctypes.c_bool
            self.input_ok = bool(hi.AXIsProcessTrusted())
        except AttributeError:
            pass
        self._held: set[int] = set()
        self._flags = 0
        self._pos = CGPoint(0, 0)
        self._last_click = (0.0, -1, 0)      # time, button, count

    def _post(self, ev) -> None:
        if ev:
            self.cg.CGEventPost(HID_TAP, ev)
            self.cf.CFRelease(ev)

    def _require_input(self) -> None:
        if not self.input_ok:
            raise DesktopUnavailable("macOS has not allowed Cloudora to control the keyboard and mouse. On the Mac: "
                                     "System Settings > Privacy & Security > Accessibility, turn on the app Cloudora "
                                     "runs in, then restart Cloudora.")

    def move(self, screen: int, fx: float, fy: float) -> None:
        self._require_input()
        m = self.monitor(screen)
        self._pos = CGPoint(m["left"] + fx * (m["width"] - 1), m["top"] + fy * (m["height"] - 1))
        held = next(iter(self._held), None)
        kind = DRAG[held] if held is not None else MOVED
        ev = self.cg.CGEventCreateMouseEvent(None, kind, self._pos, CG_BUTTON[held] if held is not None else 0)
        self._post(ev)

    def nudge(self, dx: int, dy: int) -> None:
        self._require_input()
        bounds = [self.monitor(i) for i in range(len(self.monitors or [None]))] or [self.monitor(0)]
        left = min(m["left"] for m in bounds)
        top = min(m["top"] for m in bounds)
        right = max(m["left"] + m["width"] for m in bounds) - 1
        bottom = max(m["top"] + m["height"] for m in bounds) - 1
        self._pos = CGPoint(min(right, max(left, self._pos.x + dx)), min(bottom, max(top, self._pos.y + dy)))
        held = next(iter(self._held), None)
        kind = DRAG[held] if held is not None else MOVED
        ev = self.cg.CGEventCreateMouseEvent(None, kind, self._pos, CG_BUTTON[held] if held is not None else 0)
        if ev:
            self.cg.CGEventSetIntegerValueField(ev, 4, int(dx))    # kCGMouseEventDeltaX
            self.cg.CGEventSetIntegerValueField(ev, 5, int(dy))    # kCGMouseEventDeltaY
        self._post(ev)

    def button(self, button: int, down: bool) -> None:
        self._require_input()
        now = time.monotonic()
        last_t, last_b, count = self._last_click
        if down:
            count = count + 1 if last_b == button and now - last_t < 0.4 else 1
            self._last_click = (now, button, count)
            self._held.add(button)
        else:
            self._held.discard(button)
        ev = self.cg.CGEventCreateMouseEvent(None, (DOWN if down else UP)[button], self._pos, CG_BUTTON[button])
        if ev:
            self.cg.CGEventSetIntegerValueField(ev, CLICK_STATE, max(1, count))
        self._post(ev)

    def wheel(self, dx: int, dy: int) -> None:
        self._require_input()
        # pixel units; one notch (120) is about 40 points. Positive Quartz values scroll up/left.
        self._post(self.cg.CGEventCreateScrollWheelEvent2(None, 0, 2, -dy * 40 // 120, -dx * 40 // 120, 0))

    def key(self, key: Key, down: bool) -> None:
        self._require_input()
        if key.mac is None:
            return
        flag = FLAG.get(key.mac, 0)
        if flag:
            self._flags = self._flags | flag if down else self._flags & ~flag
        ev = self.cg.CGEventCreateKeyboardEvent(None, key.mac, down)
        if ev:
            self.cg.CGEventSetFlags(ev, self._flags)
        self._post(ev)

    def cursor(self, screen: int) -> tuple[float, float] | None:
        ev = self.cg.CGEventCreate(None)
        if not ev:
            return None
        p = self.cg.CGEventGetLocation(ev)
        self.cf.CFRelease(ev)
        m = self.monitor(screen)
        fx, fy = (p.x - m["left"]) / max(1, m["width"]), (p.y - m["top"]) / max(1, m["height"])
        return (fx, fy) if 0 <= fx <= 1 and 0 <= fy <= 1 else None
