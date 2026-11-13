"""The host computer's screen, mouse and keyboard: one backend per platform.

* Windows and macOS: the screen is read with ``mss``; input goes through the operating system's own input API
  (``SendInput`` / Quartz events). macOS asks once for Screen Recording and Accessibility permission.
* Linux on X11: ``mss`` and the XTest extension.
* Linux on Wayland (GNOME, KDE, ...): the desktop's own remote-desktop portal (it asks someone at the computer to
  allow it the first time; the permission is remembered) and PipeWire for the picture.

Every backend gives the same small interface (``Backend``). Pointer positions are fractions of the screen (0..1), so
the viewer never needs to know the host's resolution or scaling. Calls may block: Cloudora runs them on its own
capture thread, never on the web server's event loop.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from cloudora.desktop.keys import Key


class DesktopUnavailable(Exception):
    """The screen cannot be shared right now. The message is shown to the viewer and says what to do."""


@dataclass(frozen=True)
class Screen:
    index: int
    name: str
    width: int
    height: int


@dataclass
class Frame:
    """One captured picture: ``width`` x ``height`` pixels, 4 bytes each (B, G, R, unused), rows ``stride`` apart."""
    width: int
    height: int
    data: bytes | bytearray | memoryview
    stride: int = 0

    def __post_init__(self) -> None:
        if not self.stride:
            self.stride = self.width * 4


class Backend:
    """What every platform provides. All coordinates are fractions (0..1) of the chosen screen."""

    name = "?"
    cursor_in_frames = False      # True when the pointer is drawn into the pictures (else Cloudora sends its position)
    host_repeats = False          # True when the host repeats held keys itself (else the viewer sends repeats)

    def screens(self) -> list[Screen]:
        raise NotImplementedError

    def grab(self, screen: int, timeout: float) -> Frame | None:
        """The current picture of ``screen``, or None if nothing new arrived within ``timeout`` seconds."""
        raise NotImplementedError

    def cursor(self, screen: int) -> tuple[float, float] | None:
        """Where the host's pointer is (fractions of the screen), or None if unknown or not on this screen."""
        return None

    def move(self, screen: int, fx: float, fy: float) -> None:
        raise NotImplementedError

    def nudge(self, dx: int, dy: int) -> None:
        """Move the pointer by (dx, dy) pixels relative to where it is - what games read (mouse look). The viewer
        sends these while the mouse is captured (Ctrl+Alt+M, for games)."""
        raise NotImplementedError

    def button(self, button: int, down: bool) -> None:
        """``button``: 0 left, 1 middle, 2 right."""
        raise NotImplementedError

    def wheel(self, dx: int, dy: int) -> None:
        """Scroll by wheel units: 120 = one notch. Positive ``dy`` scrolls down, positive ``dx`` right."""
        raise NotImplementedError

    def key(self, key: Key, down: bool) -> None:
        raise NotImplementedError

    def gpu_codecs(self) -> list[str]:
        """Codecs this backend can encode on the GPU without copying the picture (see gpu.py); none by default."""
        return []

    def open_gpu_encoder(self, kind: str, preset: str, fps: int):
        raise DesktopUnavailable("This screen has no zero-copy GPU encoder.")

    def close(self) -> None:
        pass


class WheelAccumulator:
    """Turns fine-grained scrolling (trackpads send small amounts) into whole notches for platforms that need them."""

    def __init__(self) -> None:
        self.x = self.y = 0

    def add(self, dx: int, dy: int) -> tuple[int, int]:
        self.x += dx
        self.y += dy
        sx, sy = int(self.x / 120), int(self.y / 120)
        self.x -= sx * 120
        self.y -= sy * 120
        return sx, sy


def platform_kind() -> str:
    forced = os.environ.get("CLOUDORA_DESKTOP", "").strip().lower()
    if forced:
        return forced
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or (os.environ.get("WAYLAND_DISPLAY")
                                                           and not os.environ.get("DISPLAY")):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "none"


def open_backend(data_dir: Path, status=lambda _msg: None, cancel=None) -> Backend:
    """Open this computer's screen. ``status`` receives progress messages (e.g. waiting for permission); setting the
    ``cancel`` event (a threading.Event) makes a pending permission request give up."""
    kind = platform_kind()
    if kind == "windows":
        from cloudora.desktop.windows import WindowsBackend
        return WindowsBackend()
    if kind == "macos":
        from cloudora.desktop.macos import MacBackend
        return MacBackend()
    if kind == "x11":
        from cloudora.desktop.x11 import X11Backend
        return X11Backend()
    if kind == "wayland":
        from cloudora.desktop.wayland import WaylandBackend
        return WaylandBackend(data_dir / "wayland-restore-token", status, cancel)
    raise DesktopUnavailable("Cloudora is not running inside a graphical desktop session on this computer. Start it "
                             "from a terminal in the desktop (not over SSH or as a system service).")
