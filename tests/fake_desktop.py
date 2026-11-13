"""A stand-in screen for tests: synthetic pictures and a record of every input event."""

from __future__ import annotations

import threading

from cloudora.desktop import Backend, DesktopUnavailable, Frame, Screen
from cloudora.desktop.keys import Key


def solid(width: int, height: int, bgr: tuple[int, int, int]) -> bytearray:
    return bytearray(bytes((*bgr, 0)) * (width * height))


def paint(buf: bytearray, width: int, x: int, y: int, w: int, h: int, bgr: tuple[int, int, int]) -> None:
    px = bytes((*bgr, 0)) * w
    for row in range(y, y + h):
        start = (row * width + x) * 4
        buf[start:start + w * 4] = px


class FakeBackend(Backend):
    name = "Fake screen"
    cursor_in_frames = False

    def __init__(self, width: int = 320, height: int = 200):
        self.width, self.height = width, height
        self.picture = solid(width, height, (40, 30, 20))
        self.events: list[tuple] = []
        self.version = 1          # bumped by change(); grab() returns a frame only when it changed
        self._sent = 0
        self.lock = threading.Lock()
        self.closed = False
        self.fail_grabs = 0
        self.cursor_pos: tuple[float, float] | None = (0.25, 0.5)

    def change(self, x: int, y: int, w: int, h: int, bgr=(0, 200, 255)) -> None:
        with self.lock:
            paint(self.picture, self.width, x, y, w, h, bgr)
            self.version += 1

    def screens(self) -> list[Screen]:
        return [Screen(0, f"Fake ({self.width}×{self.height})", self.width, self.height),
                Screen(1, "Second fake", self.width, self.height)]

    def grab(self, screen: int, timeout: float) -> Frame | None:
        if self.fail_grabs:
            self.fail_grabs -= 1
            raise DesktopUnavailable("fake screen went away")
        with self.lock:
            if self._sent == self.version:
                return None
            self._sent = self.version
            return Frame(self.width, self.height, bytes(self.picture))

    def cursor(self, screen: int):
        return self.cursor_pos

    def move(self, screen: int, fx: float, fy: float) -> None:
        self.events.append(("move", screen, round(fx, 3), round(fy, 3)))

    def nudge(self, dx: int, dy: int) -> None:
        self.events.append(("nudge", dx, dy))

    def button(self, button: int, down: bool) -> None:
        self.events.append(("button", button, down))

    def wheel(self, dx: int, dy: int) -> None:
        self.events.append(("wheel", dx, dy))

    def key(self, key: Key, down: bool) -> None:
        self.events.append(("key", key.code, down))

    def close(self) -> None:
        self.closed = True


class Opener:
    """Replaces open_backend: hands out one FakeBackend (counting how often the screen was opened)."""

    def __init__(self, backend: FakeBackend | None = None, error: str = ""):
        self.backend = backend or FakeBackend()
        self.error = error
        self.opened = 0

    def __call__(self, data_dir, status=lambda _m: None, cancel=None) -> FakeBackend:
        self.opened += 1
        if self.error:
            raise DesktopUnavailable(self.error)
        self.backend.closed = False
        self.backend._sent = 0
        return self.backend
