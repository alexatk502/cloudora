"""Sound: what the host computer plays, sent to the viewer as Opus in 10 ms packets (low latency, the default of
game-streaming hosts such as Selkies). Nothing is recorded; packets go out as they are made.

* Linux: PipeWire captures the speakers' output (the "monitor"), GStreamer encodes Opus - all in native threads.
* Windows: WASAPI loopback (the ``soundcard`` package) and PyAV's Opus encoder.
* macOS cannot capture what it plays without an extra virtual audio device, so sound is not offered there.
"""

from __future__ import annotations

import sys
import threading

FRAME_MS = 10
RATE, CHANNELS, BITRATE = 48000, 2, 128000


class GstAudio:
    """Linux: PipeWire (speakers' monitor) -> Opus, in GStreamer."""

    def __init__(self) -> None:
        from cloudora.desktop import gpu
        loaded = gpu._load()
        if not loaded:
            raise RuntimeError("GStreamer bindings missing")
        self.Gst = loaded[0]
        self.pipe = self.Gst.parse_launch(
            "pipewiresrc stream-properties=props,stream.capture.sink=true,media.class=Stream/Input/Audio,"
            "node.name=cloudora-sound ! audioconvert ! audioresample ! "
            f"audio/x-raw,rate={RATE},channels={CHANNELS} ! "
            f"opusenc frame-size={FRAME_MS} bitrate={BITRATE} audio-type=restricted-lowdelay ! "
            "appsink name=out sync=false max-buffers=25 drop=true emit-signals=false")
        self.out = self.pipe.get_by_name("out")
        if self.pipe.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("sound capture could not start")

    def read(self, timeout: float) -> bytes | None:
        sample = self.out.try_pull_sample(int(timeout * 1e9))
        if sample is None:
            return None
        buf = sample.get_buffer()
        ok, info = buf.map(self.Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            return bytes(info.data)
        finally:
            buf.unmap(info)

    def __del__(self) -> None:
        self.close()

    def close(self) -> None:
        pipe, self.pipe = getattr(self, "pipe", None), None
        if pipe is not None:
            pipe.set_state(self.Gst.State.NULL)


class LoopbackAudio:
    """Windows: WASAPI loopback of the default speakers, Opus via PyAV. (Not tested on Windows yet.)"""

    def __init__(self) -> None:
        import av
        import soundcard
        speaker = soundcard.default_speaker()
        mic = soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
        self.frames = RATE * FRAME_MS // 1000
        self.recorder = mic.recorder(samplerate=RATE, channels=CHANNELS, blocksize=self.frames)
        self.recorder.__enter__()
        self.ctx = av.CodecContext.create("libopus", "w")
        self.ctx.sample_rate, self.ctx.layout, self.ctx.format = RATE, "stereo", "flt"
        self.ctx.bit_rate = BITRATE
        self.ctx.options = {"frame_duration": str(FRAME_MS), "application": "lowdelay"}
        self.ctx.open()
        self._av = av
        self._pts = 0

    def read(self, timeout: float) -> bytes | None:
        data = self.recorder.record(numframes=self.frames)          # float32, frames x channels
        frame = self._av.AudioFrame.from_ndarray(data.astype("float32").reshape(1, -1), format="flt",
                                                 layout="stereo")
        frame.sample_rate, frame.pts = RATE, self._pts
        self._pts += self.frames
        packets = self.ctx.encode(frame)
        return b"".join(bytes(p) for p in packets) if packets else None

    def close(self) -> None:
        rec, self.recorder = getattr(self, "recorder", None), None
        if rec is not None:
            try:
                rec.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


_available: bool | None = None
_lock = threading.Lock()


def open_source():
    """The host's sound, or None where it cannot be captured."""
    if sys.platform.startswith("linux"):
        return GstAudio()
    if sys.platform == "win32":
        return LoopbackAudio()
    raise RuntimeError("this system cannot capture the sound it plays")


def available() -> bool:
    """Can sound be sent from here? (Checked once by opening and closing the capture.)"""
    global _available
    with _lock:
        if _available is None:
            try:
                open_source().close()
                _available = True
            except Exception:  # noqa: BLE001
                _available = False
        return _available
