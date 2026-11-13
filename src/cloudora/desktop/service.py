"""Streams the host's screen to one signed-in viewer and plays their mouse and keyboard back - with low latency.

Latency and smoothness:
* Pipelined frames. Cloudora does not wait for each picture to arrive before sending the next: several are in flight
  at once - about two of the link's base delays' worth of data (flow.py) - and the viewer acknowledges each picture
  once it is on screen. That fills the link without ever building a queue, so the viewer always sees the newest
  screen. A picture is captured only when it may be sent, so it is fresh when it leaves.
* Only changed areas are sent (frames.py), as JPEG. When the link cannot keep up Cloudora lowers JPEG
  quality, then resolution, and raises them again when the link is clear; once the screen is still it sends one
  sharp picture.
* Input never waits behind pictures: it runs on its own thread, separate from capture and encoding.

One viewer controls the screen at a time; a new sign-in that connects takes over (the earlier one is told). Keys and
buttons still held when a viewer leaves are released. Nothing is recorded or written to disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cloudora.desktop import Backend, DesktopUnavailable, audio, open_backend, video
from cloudora.desktop.flow import Flow
from cloudora.desktop.frames import Encoder, pack
from cloudora.desktop.keys import KEYS
from cloudora.desktop.video import PRESETS, VideoEncoder, even, pack_video

log = logging.getLogger("cloudora.desktop")

MAX_PRESSURE = 6                 # GPU path: Auto may lower quality by up to this many notches while congested
GAME_MIN_KBPS, GAME_MAX_KBPS = 1500, 20000
GPU_CHANGE_EVERY = 3.0           # VA-API restarts its stream (keyframe) on a quality/bitrate change: change rarely
# Games' resolution ladder (while the mouse is captured). Live platforms pair each bitrate with a size (Twitch:
# 1080p60 / 720p60 / 480p): a picture needs enough bits per pixel to stay sharp, so when the bitrate falls, fewer,
# sharper pixels beat many smeared ones. Steps of the viewer's size, and the bits per pixel each codec needs
# (Twitch's 1080p60 at 6 Mb/s is ~0.05 for H.264; HEVC and AV1 need roughly 30-40 % less).
LADDER = (1.0, 0.75, 0.5)
BITS_PER_PIXEL = {"h264": 0.05, "hevc": 0.035, "av1": 0.03, "vp9": 0.035}

MAX_MESSAGE = 4096
MAX_MESSAGES_PER_SECOND = 1000   # gaming mice report up to 1000 times a second (the viewer coalesces to 250)
IDLE_CLOSE_SECONDS = 30          # keep the screen open briefly after a viewer leaves (quick reconnects)
REFINE_AFTER = 0.6               # still screen for this long after low-quality pictures: send one sharp picture
ADAPT_EVERY = 0.5                # seconds between Auto adjustments
MIN_QUALITY, MIN_SCALE = 40, 0.5
MAX_KBPS, MIN_KBPS = 30000, 300  # H.264 bitrate cap range (Auto moves within it as the link's speed changes)
REFINE_PICTURES = 3              # H.264: after a change, encode the still picture this many more times to sharpen it


@dataclass
class Settings:
    max_w: int = 1920
    max_h: int = 1080
    quality: int = 80            # the target (Auto may go lower while the link is busy)
    auto: bool = True
    fps: int = 30
    screen: int = 0
    codec: str = "jpeg"          # "av1", "hevc", "h264" or "vp9": the best both sides support (video.plan)
    engine: str = ""             # "gpu" (zero copy, gpu.py), "hw" (GPU via PyAV) or "sw" (software)
    preset: str = "auto"


class Closed(Exception):
    """The viewer is gone (disconnected, taken over, or signed out)."""


class DesktopService:
    def __init__(self, data_dir: Path, opener: Callable[..., Backend] | None = None, view_only: bool = False):
        self.data_dir = data_dir
        self.opener = opener or open_backend
        self.view_only = view_only
        self.capture_thread = ThreadPoolExecutor(1, thread_name_prefix="cloudora-capture")
        self.input_thread = ThreadPoolExecutor(1, thread_name_prefix="cloudora-input")
        self.backend: Backend | None = None
        self.last_frame = None           # the newest picture of the screen (for viewers that just connected)
        self.viewer: Viewer | None = None
        self._opening: asyncio.Future | None = None
        self.cancel_open = threading.Event()   # set when Cloudora stops: a pending permission request gives up
        self._idle_close: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------ the screen
    async def ensure_backend(self, status: Callable[[str], None]) -> Backend:
        if self._idle_close is not None:
            self._idle_close.cancel()
            self._idle_close = None
        if self.backend is not None:
            return self.backend
        loop = asyncio.get_running_loop()
        if self._opening is None:
            def report(msg: str) -> None:
                viewer = self.viewer
                if viewer is not None:
                    loop.call_soon_threadsafe(viewer.notify, "info", msg)
            self._opening = loop.run_in_executor(self.capture_thread, self.opener, self.data_dir, report,
                                                 self.cancel_open)
        opening = self._opening
        try:
            self.backend = await asyncio.shield(opening)
            log.info("screen opened (%s)", self.backend.name)
            return self.backend
        finally:
            if opening.done() and self._opening is opening:
                self._opening = None

    async def drop_backend(self) -> None:
        backend, self.backend = self.backend, None
        self.last_frame = None
        if backend is not None:
            await asyncio.get_running_loop().run_in_executor(self.capture_thread, backend.close)
            log.info("screen closed")

    def _schedule_idle_close(self) -> None:
        if self._idle_close is not None:
            self._idle_close.cancel()
        loop = asyncio.get_running_loop()
        self._idle_close = loop.call_later(IDLE_CLOSE_SECONDS, lambda: loop.create_task(self._close_if_idle()))

    async def _close_if_idle(self) -> None:
        self._idle_close = None
        if self.viewer is None:
            await self.drop_backend()

    async def preflight(self) -> str:
        """Open and close the screen once at start-up, so permission prompts appear while someone is at the computer.
        Returns a one-line description for the start-up banner."""
        try:
            backend = await self.ensure_backend(lambda _m: None)
            loop = asyncio.get_running_loop()
            screens = await loop.run_in_executor(self.capture_thread, backend.screens)
            plan = await loop.run_in_executor(self.capture_thread, self.encoders, backend)   # probe encoders now
            names = {"gpu": "GPU, zero copy", "hw": "GPU", "sw": "software"}
            text = f"{backend.name}: " + ", ".join(s.name for s in screens) + "\n  VIDEO: " + (
                ", ".join(f"{k.upper()} ({names[e]})" for k, e in plan) if plan else "JPEG only (install PyAV)")
        except DesktopUnavailable as exc:
            return f"NOT READY - {exc}"
        if self.viewer is None:
            await self.drop_backend()
        return text

    async def close(self) -> None:
        self.cancel_open.set()
        viewer = self.viewer
        if viewer is not None:
            await viewer.end("Cloudora is stopping.")
            # Let the viewer's own cleanup finish (it stops its encoders on the capture thread) before the threads
            # go away - a GStreamer pipeline left running and later discarded can crash the process.
            task = viewer.task
            if task is not None and not task.done() and task is not asyncio.current_task():
                with contextlib.suppress(Exception, asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), 5)
        if self._idle_close is not None:
            self._idle_close.cancel()
        opening = self._opening
        if opening is not None:      # a screen still being opened (e.g. waiting for permission): let it give up
            try:
                self.backend = await asyncio.wait_for(asyncio.shield(opening), 5)
            except (Exception, asyncio.TimeoutError):  # noqa: BLE001
                pass
        await self.drop_backend()
        self.capture_thread.shutdown(wait=False, cancel_futures=True)
        self.input_thread.shutdown(wait=False, cancel_futures=True)

    def encoders(self, backend: Backend) -> list[tuple[str, str]]:
        """(On the capture thread.) What this computer can send, best first - probed once per opened screen."""
        if getattr(self, "_plan_for", None) is not backend:
            gpu_kinds = backend.gpu_codecs() if hasattr(backend, "gpu_codecs") else []
            self._plan = video.plan(gpu_kinds)
            self._plan_for = backend
        return list(self._plan)

    # ------------------------------------------------------------------ viewers
    async def serve(self, ws, session) -> None:
        """Run one viewer's connection until it closes. ``ws`` is an accepted Starlette WebSocket."""
        previous = self.viewer
        viewer = Viewer(self, ws, session)
        self.viewer = viewer
        if previous is not None:
            await previous.end("Another sign-in took over this screen.")
        log.info("a viewer connected")
        session.viewers += 1
        viewer.task = asyncio.current_task()
        try:
            await viewer.run()
        except asyncio.CancelledError:
            if not viewer.closed:
                raise
            viewer.task.uncancel()           # ended on purpose (new key, take-over, stopping): not an error
        finally:
            session.viewers -= 1
            await viewer.release_all()
            if self.viewer is viewer:
                self.viewer = None
                self._schedule_idle_close()
            log.info("the viewer disconnected")

    def session_ended(self, session) -> None:
        viewer = self.viewer
        if viewer is not None and viewer.session is session:
            asyncio.get_running_loop().create_task(viewer.end("You were signed out."))


class Viewer:
    def __init__(self, service: DesktopService, ws, session):
        self.service, self.ws, self.session = service, ws, session
        self.settings = Settings()
        self.encoder = Encoder()
        self.keys_down: set[str] = set()
        self.buttons_down: set[int] = set()
        self.flow = Flow()
        self.window_open = asyncio.Event()
        self.window_open.set()
        self.seq = 0
        self.scale = 1.0                 # Auto: fraction of the viewer's size actually sent
        self.last_scale = 0.0
        self.last_adapt = time.monotonic()
        self.waited = 0.0                # time spent waiting for the link since the last adjustment
        self.sent_since_adapt = 0
        self.quality = self.settings.quality
        self.degraded = False
        self.last_change = time.monotonic()
        self.last_cursor: Any = ()
        self.closed = False
        self.reason = ""
        self._send_lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.input_failed = False
        self.video: VideoEncoder | None = None
        self.gpu = None                  # zero-copy GPU encoder (gpu.GstEncoder) when the backend offers one
        self.pressure = 0                # GPU path: notches of quality given up while the link is congested
        self.game_kbps = 8000            # mouse captured (games): the GPU encoder's constant bitrate
        self.last_gpu_change = 0.0       # when quality/bitrate last changed on the GPU path (each change = a keyframe)
        self.options: list[tuple[str, str]] = []    # what this computer can send: [(codec, engine)], best first
        self.viewer_codecs: list = []                # what the viewer's browser decodes
        self.configured = asyncio.Event()            # the viewer said what it can decode
        self.sound: asyncio.Task | None = None       # sending the host's sound (when the viewer turned it on)
        self._sound_stop: threading.Event | None = None
        self.key_next = True             # H.264: the next picture must be a keyframe
        self.refine_left = 0
        self.max_kbps = 8000             # H.264 bitrate cap; Auto raises it while the link keeps up
        self.last_raw = None
        self.last_key = 0.0              # when the last keyframe went out (a big picture: briefly looks like congestion)
        self.key_until = 0.0             # ...and until when its queue is expected (keyframe size / link speed)
        self.congested_streak = 0

    # ------------------------------------------------------------------ sending
    async def send_text(self, obj: dict) -> None:
        if self.closed:
            raise Closed
        async with self._send_lock:
            await self.ws.send_text(json.dumps(obj))

    def notify(self, kind: str, msg: str) -> None:
        if not self.closed:
            asyncio.get_running_loop().create_task(self._quiet(self.send_text({"t": kind, "msg": msg})))

    @staticmethod
    async def _quiet(aw) -> None:
        try:
            await aw
        except Exception:  # noqa: BLE001 - the viewer may already be gone
            pass

    async def end(self, reason: str) -> None:
        if self.closed:
            return
        try:
            await self.send_text({"t": "bye", "msg": reason})
        except Exception:  # noqa: BLE001
            pass
        self.closed, self.reason = True, reason
        try:
            await self.ws.close(code=4000)
        except Exception:  # noqa: BLE001
            pass
        task = self.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()                     # stop waiting (e.g. for the screen to open) right away

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        try:
            await self.send_text({"t": "info", "msg": "Opening the screen…"})
            backend = await self.service.ensure_backend(lambda _m: None)
            loop = asyncio.get_running_loop()
            self.options = await loop.run_in_executor(self.service.capture_thread, self.service.encoders, backend)
            await self.send_text({"t": "hello", "backend": backend.name, "cursor": not backend.cursor_in_frames,
                                  "repeat": backend.host_repeats,
                                  "view_only": self.service.view_only, "video": bool(self.options),
                                  "codecs": list(dict.fromkeys(k for k, _ in self.options)),
                                  "audio": await loop.run_in_executor(self.service.capture_thread, audio.available)})
        except DesktopUnavailable as exc:
            await self._quiet(self.send_text({"t": "err", "msg": str(exc)}))
            return
        except Closed:
            return
        tasks = [asyncio.ensure_future(self._pictures()), asyncio.ensure_future(self._receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if isinstance(exc, DesktopUnavailable):
                    await self._quiet(self.send_text({"t": "err", "msg": str(exc)}))
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._stop_sound()
            for enc in (self.video, self.gpu):
                if enc is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.get_running_loop().run_in_executor(self.service.capture_thread, enc.close)
            self.video = self.gpu = None

    def _adapt(self, now: float) -> None:
        """Auto: keep up with the link. If Cloudora spent much of the last moment waiting for it, or a queue is building,
        send smaller pictures (quality first, then resolution); when the link is clear, go back up."""
        s, period = self.settings, now - self.last_adapt
        if period < ADAPT_EVERY:
            return
        busy = self.waited / period
        self.last_adapt, self.waited = now, 0.0
        # Congestion = a queue is building (the round trip grows past the link's base delay). Waiting for the send
        # window alone is not: with small pictures the window is small simply because little is being sent.
        queue = self.flow.queue_delay()
        jitter = min(3 * self.flow.jitter(), 0.1)      # Wi-Fi swings the round trip: that alone is no queue
        congested = queue > max(0.08, jitter) or (busy > 0.4 and queue > max(0.03, jitter))
        clear = queue < max(0.03, jitter)
        if now < self.key_until:
            congested = False            # a keyframe is still draining: not a sign that the link got slower
        self.congested_streak = self.congested_streak + 1 if congested else 0
        if s.engine == "gpu" and s.preset == "game":
            # Games (mouse captured): the constant bitrate follows the link (congestion control, as Selkies
            # does): down by a quarter when a queue builds, up gently when clear. The size follows the bitrate in
            # steps (the ladder, as live platforms do), changing together with it - one keyframe for both, at most
            # every few seconds.
            if now - self.last_gpu_change < GPU_CHANGE_EVERY:
                return
            new = self.game_kbps
            if self.congested_streak >= 2:
                rate = self.flow.rate(now)
                target = rate * 8 / 1000 * 0.85 if rate else self.game_kbps * 0.75
                new = int(max(GAME_MIN_KBPS, min(self.game_kbps * 0.75, target)))
            elif clear and self.congested_streak == 0:
                new = int(min(GAME_MAX_KBPS, self.game_kbps * 1.25))
            rung = self._rung(new, s)
            if abs(new - self.game_kbps) > 0.2 * self.game_kbps or rung != self.scale:   # changes worth a keyframe
                self.game_kbps, self.scale, self.last_gpu_change = new, rung, now
            return
        if s.engine == "gpu":
            # Zero-copy GPU: quality and size can change live, but each change restarts the stream (a keyframe),
            # so they change at most every few seconds and only on sustained congestion; brief dips are left to
            # flow control (a few pictures fewer), which costs nothing.
            if now - self.last_gpu_change < GPU_CHANGE_EVERY:
                return
            before = (self.pressure, self.scale)
            if self.congested_streak >= 2:
                self.pressure = min(MAX_PRESSURE, self.pressure + 1)
                if self.pressure >= MAX_PRESSURE and s.auto and self.scale > MIN_SCALE and now - self.last_scale > 2:
                    self.scale, self.last_scale = max(MIN_SCALE, round(self.scale * 0.8, 3)), now
            elif clear:
                if s.auto and self.scale < 1 and self.pressure == 0 and now - self.last_scale > 4:
                    self.scale, self.last_scale = min(1.0, round(self.scale / 0.8, 3)), now
                self.pressure = max(0, self.pressure - 1)
            if (self.pressure, self.scale) != before:
                self.last_gpu_change = now
            return
        if s.codec == "h264" and s.engine == "sw":
            pass                             # x264: the bitrate cap follows the link (below)
        elif s.codec != "jpeg":
            # Other encoders keep their cap per stream, so when the link stays congested Auto sends a
            # smaller picture (a new stream), and a bigger one again once it is clear.
            if self.congested_streak >= 3 and s.auto and self.scale > MIN_SCALE and now - self.last_scale > 3:
                self.scale, self.last_scale = max(MIN_SCALE, round(self.scale * 0.8, 3)), now
            elif clear and s.auto and self.scale < 1 and now - self.last_scale > 5:
                self.scale, self.last_scale = min(1.0, round(self.scale / 0.8, 3)), now
            rate = self.flow.rate(now)
            if congested and rate:
                self.max_kbps = int(max(MIN_KBPS, rate * 8 / 1000 * 0.8))
            elif clear:
                self.max_kbps = int(min(MAX_KBPS, self.max_kbps * 1.25))
            return
        if s.codec == "h264" and s.engine == "sw":
            # H.264 cannot skip pictures, so its bitrate always follows the link: at congestion, just under what the
            # link delivers; when clear, up again. Resolution only drops (Auto) when even a low bitrate is too much.
            if congested:
                rate = self.flow.rate(now)
                target = rate * 8 / 1000 * 0.7 if rate else self.max_kbps * 0.7
                if self.max_kbps <= MIN_KBPS * 2 and s.auto and self.scale > MIN_SCALE and now - self.last_scale > 2:
                    self.scale, self.last_scale = max(MIN_SCALE, round(self.scale * 0.8, 3)), now
                self.max_kbps = int(max(MIN_KBPS, min(self.max_kbps * 0.8, target)))
            elif clear:
                if s.auto and self.scale < 1 and self.max_kbps >= MIN_KBPS * 6 and now - self.last_scale > 4:
                    self.scale, self.last_scale = min(1.0, round(self.scale / 0.8, 3)), now
                self.max_kbps = int(min(MAX_KBPS, self.max_kbps * 1.25))
            return
        if not s.auto:
            return
        if congested:
            if self.quality > MIN_QUALITY:
                self.quality = max(MIN_QUALITY, self.quality - 10)
            elif self.scale > MIN_SCALE and now - self.last_scale > 2:
                self.scale, self.last_scale = max(MIN_SCALE, round(self.scale * 0.8, 3)), now
            self.degraded = True
        elif clear:
            if self.scale < 1 and now - self.last_scale > 4:
                self.scale, self.last_scale = min(1.0, round(self.scale / 0.8, 3)), now
            elif self.quality < s.quality:
                self.quality = min(s.quality, self.quality + 5)

    def _rung(self, kbps: int, s: Settings) -> float:
        """Games: the largest step of the ladder this bitrate keeps sharp. Going up needs a margin (30 %), so the
        size does not flip back and forth around a boundary."""
        if not s.auto:
            return self.scale
        full = s.max_w * s.max_h
        nw, nh = getattr(self.gpu, "native", None) or (0, 0)
        if nw and nh:                                    # the screen may be smaller than the viewer's window
            k = min(1.0, s.max_w / nw, s.max_h / nh)
            full = nw * nh * k * k
        need = BITS_PER_PIXEL.get(s.codec, 0.05) * full * s.fps / 1000      # kb/s for the full size
        for step in LADDER:
            margin = 1.3 if step > self.scale else 1.0
            if kbps >= need * step * step * margin:
                return step
        return LADDER[-1]

    async def _pictures(self) -> None:
        service, loop = self.service, asyncio.get_running_loop()
        failures = 0
        with contextlib.suppress(asyncio.TimeoutError):   # start in the right format, not JPEG first and then video
            await asyncio.wait_for(self.configured.wait(), 2)
        while not self.closed:
            interval = 1 / self.settings.fps
            self._adapt(time.monotonic())
            if not self.flow.may_send(self.settings.fps, time.monotonic()):
                self.window_open.clear()
                waited = time.monotonic()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.window_open.wait(), 0.5)
                self.waited += time.monotonic() - waited
                continue
            started = time.monotonic()
            backend = service.backend
            if backend is None:
                backend = await service.ensure_backend(lambda _m: None)
            s = self.settings
            try:
                result = await loop.run_in_executor(service.capture_thread, self._capture, backend, s, interval)
                failures = 0
            except DesktopUnavailable as exc:
                failures += 1
                if failures > 3:
                    raise
                await self._quiet(self.send_text({"t": "info", "msg": f"{exc} Reconnecting to the screen…"}))
                await service.drop_backend()
                await asyncio.sleep(1)
                await service.ensure_backend(lambda _m: None)
                self.encoder.refresh()
                self.key_next = True
                continue
            payload, cursor = result
            now = time.monotonic()
            if cursor != self.last_cursor:
                self.last_cursor = cursor
                await self.send_text({"t": "cur", "x": cursor[0], "y": cursor[1]} if cursor else {"t": "cur"})
            if payload is not None:
                self.seq += 1
                data = (pack(self.seq, *payload[1:]) if payload[0] == "jpeg" else
                        pack_video(self.seq, *payload[1:], at_ms=int(now * 1000)))
                if payload[0] == "video" and payload[3]:
                    self.last_key = now
                    rate = self.flow.rate(now) or 250_000          # bytes/s; unknown yet: assume a slow link
                    self.key_until = now + max(1.5, 2 * len(data) / rate)
                self.flow.sent(self.seq, len(data), now)
                async with self._send_lock:
                    if self.closed:
                        return
                    await self.ws.send_bytes(data)
                self.last_change = now
            elif s.codec == "jpeg":
                if self.degraded and now - self.last_change > REFINE_AFTER:
                    self.quality, self.degraded = s.quality, False   # still screen: one sharp picture
                    self.encoder.refresh()
            if self.session is not None and now - getattr(self, "_touched", 0) > 30:
                self.session.touch()
                self._touched = now
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    def _capture(self, backend: Backend, s: Settings, interval: float):
        """On the capture thread: wait for the next picture and encode it (H.264) or what changed in it (JPEG)."""
        cursor = None if backend.cursor_in_frames else backend.cursor(s.screen)
        if cursor is not None:
            cursor = (round(cursor[0], 4), round(cursor[1], 4))
        if s.codec != "jpeg":
            return self._capture_video(backend, s, interval), cursor
        if self.gpu is not None:              # JPEG needs the raw pictures: the GPU encoder lets go of the stream
            self.gpu.close()
            self.gpu = None
        frame = backend.grab(s.screen, interval if not self.encoder.full_next else 0)
        if frame is not None:
            self.service.last_frame = frame
        elif self.encoder.full_next:
            # Some screens only deliver a picture when something changes (Wayland): a new viewer, a refresh, new
            # settings or the sharp picture after a busy moment re-encode the last one instead of waiting.
            frame = self.service.last_frame
        payload = None
        if frame is not None:
            encoded = self.encoder.encode(frame, max(160, int(s.max_w * self.scale)),
                                          max(120, int(s.max_h * self.scale)), self.quality)
            payload = ("jpeg", *encoded) if encoded is not None else None
        return payload, cursor

    def _capture_gpu(self, backend: Backend, s: Settings, interval: float):
        """Zero-copy GPU path: the backend's GPU encoder delivers finished pictures; nothing is copied here. If the
        GPU fails, this viewer continues with the next encoder (PyAV or software) instead."""
        try:
            return self._capture_gpu_once(backend, s, interval)
        except DesktopUnavailable as exc:
            log.warning("GPU encoding failed (%s); continuing without it", exc)
            if self.gpu is not None:
                self.gpu.close()
                self.gpu = None
            self.options = [o for o in self.options if o[1] != "gpu"]
            s.codec, s.engine = video.choose(self.options, self.viewer_codecs)
            self.key_next = True
            return None

    def _capture_gpu_once(self, backend: Backend, s: Settings, interval: float):
        if self.gpu is None or (self.gpu.kind, self.gpu.preset) != (s.codec, s.preset):
            if self.gpu is not None:
                self.gpu.close()
                self.gpu = None
            self.gpu = backend.open_gpu_encoder(s.codec, s.preset, s.fps)
            self.key_next = True
        if self.key_next:
            self.gpu.request_key()
            self.key_next = False
        self.gpu.fit(max(160, int(s.max_w * self.scale)), max(120, int(s.max_h * self.scale)))
        if s.preset == "game":
            self.gpu.set_bitrate(self.game_kbps)
        else:
            self.gpu.set_pressure(self.pressure)
        got = self.gpu.read(interval)
        if got is None:
            return None
        data, key = got
        if key and not self.gpu.codec:
            return None                       # cannot happen in practice: a keyframe always names its codec
        return "video", (self.gpu.width, self.gpu.height), data, key, self.gpu.codec

    def _capture_video(self, backend: Backend, s: Settings, interval: float):
        if s.engine == "gpu":
            return self._capture_gpu(backend, s, interval)
        if self.gpu is not None:              # switched away from the GPU: free the screen's stream for pictures
            self.gpu.close()
            self.gpu = None
        force = self.key_next
        again = force or self.refine_left > 0
        frame = backend.grab(s.screen, 0 if again else interval)
        changed = frame is not None
        if changed:
            self.service.last_frame = frame
        else:
            frame = self.service.last_frame if again else None
        if frame is None:
            return None
        if changed and not force and self.last_raw is not None and frame.data == self.last_raw:
            changed = False                            # polled screens (mss): the same picture again
            if self.refine_left <= 0:
                return None
        k = min(1.0, s.max_w * self.scale / frame.width, s.max_h * self.scale / frame.height)
        size = (even(round(frame.width * k)), even(round(frame.height * k)))
        if self.video is None or (self.video.width, self.video.height, self.video.preset, self.video.kind,
                                  self.video.engine) != (*size, s.preset, s.codec, s.engine):
            if self.video is not None:
                self.video.close()
            self.video = VideoEncoder(size[0], size[1], s.preset, s.fps, self.max_kbps, kind=s.codec,
                                      engine=s.engine)
            force = True
        self.video.set_max_kbps(self.max_kbps)
        data, key = self.video.encode(frame, key=force)
        self.key_next = False
        self.last_raw = frame.data
        self.refine_left = REFINE_PICTURES if (changed or key) else self.refine_left - 1
        if not data:
            return None
        return "video", size, data, key, self.video.codec

    def _stop_sound(self) -> None:
        if self._sound_stop is not None:
            self._sound_stop.set()
        task, self.sound = self.sound, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _send_sound(self) -> None:
        """The host's sound, Opus packets as they are made (u8 3 | u32 seq | packet). Not flow-controlled - it is
        small - but never allowed to fall behind: if packets pile up, the oldest are dropped."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        stop = self._sound_stop = threading.Event()

        def deliver(item) -> None:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)

        def pump() -> None:
            try:
                source = audio.open_source()
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(deliver, exc)
                return
            try:
                while not stop.is_set():
                    data = source.read(0.1)
                    if data:
                        loop.call_soon_threadsafe(deliver, data)
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(deliver, exc)
            finally:
                source.close()
        threading.Thread(target=pump, name="cloudora-sound", daemon=True).start()
        seq = 0
        try:
            while not self.closed:
                item = await queue.get()
                if isinstance(item, Exception):
                    self.notify("info", f"Sound is not available: {item}")
                    return
                seq += 1
                async with self._send_lock:
                    if self.closed:
                        return
                    await self.ws.send_bytes(struct.pack("<BI", 3, seq & 0xFFFFFFFF) + item)
        finally:
            stop.set()

    # ------------------------------------------------------------------ input
    async def _receive(self) -> None:
        window, count = time.monotonic(), 0
        while not self.closed:
            raw = await self.ws.receive_text()
            now = time.monotonic()
            if now - window >= 1:
                window, count = now, 0
            count += 1
            if count > MAX_MESSAGES_PER_SECOND or len(raw) > MAX_MESSAGE:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                await self.handle(msg)

    async def handle(self, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "ack":
            seq, now = msg.get("s"), time.monotonic()
            if isinstance(seq, int) and not isinstance(seq, bool):
                self.flow.acked(seq, now)
            if self.flow.may_send(self.settings.fps, now):
                self.window_open.set()
            return
        if kind == "cfg":
            self._configure(msg)
            return
        if kind == "ref":
            self.encoder.refresh()
            self.key_next = True
            return
        if self.service.view_only:
            return
        action = self._input(kind, msg)
        if action is not None:
            backend = self.service.backend
            if backend is not None:
                try:
                    await asyncio.get_running_loop().run_in_executor(self.service.input_thread, action, backend)
                except Exception as exc:  # noqa: BLE001 - one failed event must not end the session
                    if not self.input_failed:     # but say so (once per viewer): silent failure hides real bugs
                        self.input_failed = True
                        log.warning("mouse/keyboard input failed: %r", exc, exc_info=True)
                        self.notify("info", f"Control of this computer failed: {exc}")

    def _configure(self, msg: dict) -> None:
        s, old = self.settings, (self.settings.max_w, self.settings.max_h)

        def num(name: str, lo: int, hi: int, default: int) -> int:
            v = msg.get(name)
            return max(lo, min(hi, int(v))) if isinstance(v, (int, float)) and math.isfinite(v) else default
        s.max_w, s.max_h = num("w", 160, 8192, s.max_w), num("h", 120, 8192, s.max_h)
        s.quality, s.fps = num("q", 20, 95, s.quality), num("fps", 5, 60, s.fps)
        s.auto = bool(msg.get("auto", s.auto))
        if "codecs" in msg:
            self.viewer_codecs = msg["codecs"] if isinstance(msg["codecs"], list) else []
            self.configured.set()
        if "audio" in msg:
            if msg["audio"] is True and self.sound is None and not self.closed:
                self.sound = asyncio.get_running_loop().create_task(self._send_sound())
            elif msg["audio"] is not True:
                self._stop_sound()
        codec, engine = video.choose(self.options, msg["codecs"]) if "codecs" in msg else (s.codec, s.engine)
        preset = msg.get("p", s.preset)
        preset = preset if preset in PRESETS else "auto"
        if (codec, engine, preset) != (s.codec, s.engine, s.preset):
            s.codec, s.engine, s.preset = codec, engine, preset
            self.pressure = 0
            self.encoder.refresh()
            self.key_next = True
        self.quality = s.quality
        if (s.max_w, s.max_h) != old:
            self.encoder.refresh()
            self.key_next = True

    def _input(self, kind: Any, msg: dict) -> Callable[[Backend], None] | None:
        screen = self.settings.screen

        def frac(v: Any) -> float | None:
            return max(0.0, min(1.0, float(v))) if isinstance(v, (int, float)) and math.isfinite(v) else None
        if kind == "m":
            x, y = frac(msg.get("x")), frac(msg.get("y"))
            if x is None or y is None:
                return None
            return lambda b: b.move(screen, x, y)
        if kind == "r":                       # relative motion (mouse captured, for games)
            dx, dy = msg.get("dx"), msg.get("dy")
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (dx, dy)):
                return None
            dx, dy = max(-4000, min(4000, dx)), max(-4000, min(4000, dy))
            return (lambda b: b.nudge(dx, dy)) if dx or dy else None
        if kind == "b":
            button, down = msg.get("b"), msg.get("d")
            if button not in (0, 1, 2) or not isinstance(down, bool):
                return None
            (self.buttons_down.add if down else self.buttons_down.discard)(button)
            x, y = frac(msg.get("x")), frac(msg.get("y"))

            def press(b: Backend) -> None:
                if x is not None and y is not None:
                    b.move(screen, x, y)
                b.button(button, down)
            return press
        if kind == "w":
            dx, dy = msg.get("dx", 0), msg.get("dy", 0)
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (dx, dy)):
                return None
            dx, dy = max(-2400, min(2400, dx)), max(-2400, min(2400, dy))
            return lambda b: b.wheel(dx, dy)
        if kind == "k":
            key, down = KEYS.get(msg.get("c")) if isinstance(msg.get("c"), str) else None, msg.get("d")
            if key is None or not isinstance(down, bool):
                return None
            (self.keys_down.add if down else self.keys_down.discard)(key.code)
            return lambda b: b.key(key, down)
        if kind == "rel":
            return self._release_action()
        return None

    def _release_action(self) -> Callable[[Backend], None] | None:
        keys = [KEYS[c] for c in self.keys_down]
        buttons = list(self.buttons_down)
        self.keys_down.clear()
        self.buttons_down.clear()
        if not keys and not buttons:
            return None

        def release(b: Backend) -> None:
            for k in keys:
                b.key(k, False)
            for btn in buttons:
                b.button(btn, False)
        return release

    async def release_all(self) -> None:
        """Never leave a key or button held down on the host when a viewer goes away."""
        action = self._release_action()
        backend = self.service.backend
        if action is not None and backend is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(self.service.input_thread, action, backend)
            except Exception:  # noqa: BLE001
                pass
