"""Windows: pictures with mss (GDI), input with SendInput. No extra software or permissions are needed.

Cloudora marks itself DPI-aware, so pictures and pointer positions use real pixels on scaled (125 %, 150 %) screens.
Windows does not let a normal program see or control the secure desktop (the lock screen, Ctrl+Alt+Del, UAC
prompts) or windows of programs running as administrator unless Cloudora itself runs as administrator.
"""

from __future__ import annotations

import ctypes
import sys

from cloudora.desktop import DesktopUnavailable
from cloudora.desktop.keys import Key
from cloudora.desktop.mss_capture import MssBackend

ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
_BUTTON_FLAGS = {0: (0x0002, 0x0004), 1: (0x0020, 0x0040), 2: (0x0008, 0x0010)}   # (down, up)
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _dpi_aware() -> None:
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # per-monitor v2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


class WindowsBackend(MssBackend):
    name = "Windows"
    cursor_in_frames = False

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise DesktopUnavailable("The Windows backend only runs on Windows.")
        _dpi_aware()
        super().__init__()
        self.user32 = ctypes.windll.user32
        self.user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SendInput.restype = ctypes.c_uint
        self.user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.user32.MapVirtualKeyW.restype = ctypes.c_uint
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]

    def _send(self, *inputs: INPUT) -> None:
        arr = (INPUT * len(inputs))(*inputs)
        self.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    @staticmethod
    def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
        return INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))

    @staticmethod
    def _key(vk: int, scan: int, flags: int) -> INPUT:
        return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))

    def _virtual_screen(self) -> tuple[int, int, int, int]:
        g = self.user32.GetSystemMetrics
        return g(76), g(77), max(2, g(78)), max(2, g(79))   # SM_X/YVIRTUALSCREEN, SM_CX/CYVIRTUALSCREEN

    def move(self, screen: int, fx: float, fy: float) -> None:
        m = self.monitor(screen)
        x = m["left"] + fx * (m["width"] - 1)
        y = m["top"] + fy * (m["height"] - 1)
        vx, vy, vw, vh = self._virtual_screen()
        self._send(self._mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                               round((x - vx) * 65535 / (vw - 1)), round((y - vy) * 65535 / (vh - 1))))

    def nudge(self, dx: int, dy: int) -> None:
        self._send(self._mouse(MOUSEEVENTF_MOVE, int(dx), int(dy)))

    def button(self, button: int, down: bool) -> None:
        self._send(self._mouse(_BUTTON_FLAGS[button][0 if down else 1]))

    def wheel(self, dx: int, dy: int) -> None:
        events = []
        if dy:
            events.append(self._mouse(MOUSEEVENTF_WHEEL, data=-dy))       # Windows: positive = away from the user
        if dx:
            events.append(self._mouse(MOUSEEVENTF_HWHEEL, data=dx))
        if events:
            self._send(*events)

    def key(self, key: Key, down: bool) -> None:
        up = 0 if down else KEYEVENTF_KEYUP
        if key.win < 0:
            vk = -key.win
            self._send(self._key(vk, self.user32.MapVirtualKeyW(vk, 0), up))
            return
        flags = KEYEVENTF_SCANCODE | up | (KEYEVENTF_EXTENDEDKEY if key.win & 0xE000 == 0xE000 else 0)
        self._send(self._key(0, key.win & 0xFF, flags))

    def cursor(self, screen: int) -> tuple[float, float] | None:
        p = POINT()
        if not self.user32.GetCursorPos(ctypes.byref(p)):
            return None
        m = self.monitor(screen)
        fx, fy = (p.x - m["left"]) / max(1, m["width"]), (p.y - m["top"]) / max(1, m["height"])
        return (fx, fy) if 0 <= fx <= 1 and 0 <= fy <= 1 else None
