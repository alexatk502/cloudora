"""Screen pictures with ``mss`` (Windows, macOS, Linux on X11): shared by those three backends."""

from __future__ import annotations

from cloudora.desktop import Backend, DesktopUnavailable, Frame, Screen


class MssBackend(Backend):
    """``mss`` is opened lazily on the capture thread (some platforms tie it to the thread that created it)."""

    with_cursor = False

    def __init__(self) -> None:
        self._sct = None
        self.monitors: list[dict] = []     # position and size of each screen, in the platform's input coordinates

    def _mss(self):
        if self._sct is None:
            try:
                import mss
                self._sct = mss.mss(with_cursor=self.with_cursor) if self.with_cursor else mss.mss()
            except Exception as exc:  # noqa: BLE001
                raise DesktopUnavailable(f"Cannot read this computer's screen: {exc}") from exc
            self.monitors = [dict(m) for m in self._sct.monitors[1:]] or [dict(self._sct.monitors[0])]
        return self._sct

    def screens(self) -> list[Screen]:
        self._mss()
        return [Screen(i, f"Screen {i + 1} ({m['width']}×{m['height']})", m["width"], m["height"])
                for i, m in enumerate(self.monitors)]

    def monitor(self, screen: int) -> dict:
        if not self.monitors:
            self._mss()
        return self.monitors[screen] if 0 <= screen < len(self.monitors) else self.monitors[0]

    def grab(self, screen: int, timeout: float) -> Frame | None:
        sct = self._mss()
        try:
            shot = sct.grab(self.monitor(screen))
        except Exception as exc:  # noqa: BLE001 - e.g. the screen is locked or a display was unplugged
            self._sct = None
            raise DesktopUnavailable(f"Reading the screen failed ({exc}).") from exc
        raw = shot.raw
        return Frame(shot.width, shot.height, raw, len(raw) // shot.height if shot.height else shot.width * 4)

    def close(self) -> None:
        sct, self._sct = self._sct, None
        if sct is not None:
            try:
                sct.close()
            except Exception:  # noqa: BLE001
                pass
