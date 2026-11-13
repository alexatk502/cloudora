"""This computer's real screen (opt in: CLOUDORA_LIVE_DESKTOP=1). Opens the platform backend, reads pictures, and with
CLOUDORA_LIVE_INPUT=1 also moves the pointer to the middle of the screen and back.

On Linux with Wayland the first run asks someone at the computer to allow remote control."""

from __future__ import annotations

import os
import time

import pytest

from cloudora.desktop import open_backend, platform_kind
from cloudora.desktop.frames import Encoder

pytestmark = pytest.mark.skipif(os.environ.get("CLOUDORA_LIVE_DESKTOP") != "1",
                                reason="uses the real screen: set CLOUDORA_LIVE_DESKTOP=1")


def test_real_screen_pictures_and_speed(tmp_path_factory):
    data = Path_from_env() or tmp_path_factory.mktemp("live")
    backend = open_backend(data, status=print)
    try:
        screens = backend.screens()
        assert screens and screens[0].width > 0
        frames, start, first = 0, time.monotonic(), None
        enc, encoded, enc_time = Encoder(), 0, 0.0
        while time.monotonic() - start < 2:
            f = backend.grab(0, 0.1)
            if f is not None:
                frames += 1
                first = first or f
                t = time.monotonic()
                if enc.encode(f, 1920, 1200, 80) is not None:
                    encoded += 1
                enc_time += time.monotonic() - t
        assert first is not None and first.width >= 320
        print(f"\n{platform_kind()}: {backend.name}, {screens[0].name}, {frames} pictures in 2 s, "
              f"encode {1000 * enc_time / max(1, frames):.1f} ms each")
        if os.environ.get("CLOUDORA_LIVE_INPUT") == "1":
            backend.move(0, 0.5, 0.5)
            time.sleep(0.2)
            backend.move(0, 0.52, 0.5)
    finally:
        backend.close()


def Path_from_env():
    from pathlib import Path
    value = os.environ.get("CLOUDORA_LIVE_DATA_DIR")
    return Path(value) if value else None
