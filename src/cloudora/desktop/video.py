"""Video for the screen: the best encoder this computer has, in the best codec the viewer's browser decodes.

How streaming platforms make HD small, and what of it fits a live remote desktop:
* **Modern codecs** (AV1, HEVC need about half the bits of H.264) and **screen-content tools** (palette mode, intra
  block copy for text and flat colours). Used here.
* **Slow multi-pass encoding, long look-ahead, B-frames.** Much of their saving, but it needs seconds of future
  frames; a remote desktop must send each picture the moment it exists. So: no look-ahead, no B-frames, one pass.
* **Adaptive bitrate.** Used here: the stream follows the link (flow.py, service.py).
* **Hardware encoders** (what game-streaming hosts such as Sunshine use): the GPU's video engine encodes in a few
  milliseconds with little CPU, often smaller than fast software settings.

Order of preference (measured on a real desktop: scrolling, typing, a video window, ~36-37 dB):
  1. Zero-copy GPU encoding (gpu.py, Linux/Wayland with VA-API): AV1 0.33 Mb/s, HEVC 0.23 Mb/s, ~1.5 ms CPU/picture.
  2. GPU encoders through PyAV (NVIDIA NVENC, Intel Quick Sync, AMD AMF, Apple VideoToolbox) on any OS: e.g. Intel
     AV1 0.30 Mb/s, NVIDIA AV1 0.94 Mb/s, at about half the CPU of software (the picture is still copied to the GPU).
  3. Software: x264 (1.5 Mb/s, ~10 ms, steady), then VP9 and AV1 (smaller in benchmarks, but 2-5x slower per picture
     on fast motion - live, x264 gives smoother results; Sunshine likewise keeps software AV1 off).

Settings follow the low-latency practice of game-streaming hosts: no B-frames, keyframes only when a viewer needs one
(no periodic or scene-change keyframes), a rate-control buffer of one picture so no picture is much bigger than the
next, encoders' own low-delay modes (NVENC ultra-low-latency tuning, Quick Sync low-delay rate control and async
depth 1). Keyframes carry everything the browser's decoder needs (WebCodecs, no extra description).
"""

from __future__ import annotations

import os
import struct
import threading
import time
from fractions import Fraction

# Quiet the video libraries' own chatter (driver banners, encoder configuration dumps) in Cloudora's window.
os.environ.setdefault("LIBVA_MESSAGING_LEVEL", "1")   # VA-API: errors only
os.environ.setdefault("SVT_LOG", "1")                 # SVT-AV1: errors only

from cloudora.desktop import Frame

try:  # PyAV is a dependency; without it Cloudora falls back to JPEG patches
    import av
except Exception:  # noqa: BLE001
    av = None

PRESETS = ("auto", "game")   # "game" while the viewer has captured the mouse (lowest latency, constant bitrate)
KINDS = ("av1", "hevc", "h264", "vp9")

# Hardware encoders in PyAV, best first per codec. Each family's quality scale differs.
HARDWARE = {   # Intel first: on desktop content it compressed 2-3x better than NVIDIA (AV1 0.30 vs 0.94 Mb/s)
    "av1": ("av1_qsv", "av1_nvenc", "av1_amf"),
    "hevc": ("hevc_qsv", "hevc_nvenc", "hevc_amf", "hevc_videotoolbox"),
    "h264": ("h264_qsv", "h264_nvenc", "h264_amf", "h264_videotoolbox"),
}
SOFTWARE = {"h264": "libx264", "vp9": "libvpx-vp9", "av1": "libsvtav1"}
QUALITY = {   # family -> kind -> quality value (lower = better), chosen for ~36-40 dB on desktop content
    "nvenc": {"av1": 36, "hevc": 28, "h264": 23},
    "qsv": {"av1": 30, "hevc": 26, "h264": 25},
    "amf": {"av1": 120, "hevc": 28, "h264": 25},
    "videotoolbox": {"hevc": 60, "h264": 60},
    "libx264": {"h264": 23},
    "libvpx-vp9": {"vp9": 33},
    "libsvtav1": {"av1": 35},
}


def _family(encoder: str) -> str:
    return encoder if encoder.startswith("lib") else encoder.split("_", 1)[1]


def _options(encoder: str, kind: str, preset: str) -> tuple[str, dict[str, str]]:
    """(pixel format, encoder options) for low latency."""
    fam = _family(encoder)
    q = str(QUALITY[fam][kind])
    endless = {"bf": "0", "g": "1000000"}
    if fam == "nvenc":
        return "nv12", {**endless, "preset": "p4", "tune": "ull", "zerolatency": "1", "delay": "0",
                        "forced-idr": "1", "rc": "vbr", "cq": q, "multipass": "disabled"}
    if fam == "qsv":
        return "nv12", {**endless, "preset": "veryfast", "async_depth": "1", "low_delay_brc": "1",
                        "forced_idr": "1", "global_quality": q}
    if fam == "amf":
        return "nv12", {**endless, "usage": "ultralowlatency", "quality": "speed", "rc": "cqp", "qp_i": q, "qp_p": q}
    if fam == "videotoolbox":
        return "nv12", {**endless, "realtime": "1", "prio_speed": "1", "allow_sw": "0"}
    if encoder == "libx264":
        return "yuv420p", {**endless, "preset": "superfast", "tune": "zerolatency", "crf": q, "forced-idr": "1",
                           "repeat-headers": "1", "annexb": "1",
                           "x264-params": "scenecut=0:colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=off"}
    if encoder == "libvpx-vp9":
        return "yuv420p", {**endless, "deadline": "realtime", "cpu-used": "8", "crf": q, "b": "0",
                           "tune-content": "screen", "lag-in-frames": "0", "row-mt": "1"}
    return "yuv420p", {"preset": "12", "crf": q,
                       "svtav1-params": "scm=1:lookahead=0:keyint=-1:tune=0:pred-struct=1:force-key-frames=1"}


_working: dict[str, bool] = {}
_probe_lock = threading.Lock()


def works(encoder: str) -> bool:
    """Can this encoder actually encode here? (A GPU encoder exists in PyAV everywhere, but needs its GPU and
    driver.) Tried once with a small picture, then remembered."""
    with _probe_lock:
        if encoder in _working:
            return _working[encoder]
        ok = False
        if av is not None:
            try:
                kind = next(k for k, names in HARDWARE.items() if encoder in names) if not encoder.startswith("lib") \
                    else next(k for k, name in SOFTWARE.items() if name == encoder)
                ctx = _open(encoder, kind, "auto", 640, 360, 30, 4000)
                pic = av.VideoFrame(640, 360, ctx.pix_fmt)
                pic.pts = 0
                pic.pict_type = av.video.frame.PictureType.I
                ok = bool(ctx.encode(pic) + ctx.encode(None))
                del ctx                        # PyAV frees an encoder with its last reference
            except Exception:  # noqa: BLE001 - no such GPU/driver here
                ok = False
        _working[encoder] = ok
        return ok


def encoder_for(kind: str, engine: str) -> str | None:
    if engine == "hw":
        return next((e for e in HARDWARE.get(kind, ()) if works(e)), None)
    name = SOFTWARE.get(kind)
    return name if name and works(name) else None


def plan(gpu_kinds: list[str]) -> list[tuple[str, str]]:
    """What this computer can send, best first: [(codec, engine)], engine "gpu" (zero copy), "hw" or "sw".
    Software encoders always follow, so a GPU that fails mid-session has somewhere to fall back to."""
    out: list[tuple[str, str]] = []
    for kind in ("av1", "hevc"):
        if kind in gpu_kinds:
            out.append((kind, "gpu"))
        elif encoder_for(kind, "hw"):
            out.append((kind, "hw"))
    # H.264: x264 before GPU H.264 - its bitrate cap follows the link live, which kept 40 fps on a 2 Mb/s link where
    # quality-based GPU H.264 fell to 12 fps. (AV1/HEVC on the GPU stay small enough not to need that.)
    if encoder_for("h264", "sw"):
        out.append(("h264", "sw"))
    if "h264" in gpu_kinds:
        out.append(("h264", "gpu"))
    elif encoder_for("h264", "hw"):
        out.append(("h264", "hw"))
    for kind in ("vp9", "av1"):
        if encoder_for(kind, "sw"):
            out.append((kind, "sw"))
    return out


def choose(options: list[tuple[str, str]], viewer_can: object) -> tuple[str, str]:
    """The best (codec, engine) that the viewer's browser also decodes; ("jpeg", "") if none."""
    can = set(viewer_can) if isinstance(viewer_can, (list, tuple)) else set()
    return next(((k, e) for k, e in options if k in can), ("jpeg", ""))


def codec_string(kind: str, packet: bytes, width: int, height: int) -> str:
    """The WebCodecs codec name for this stream."""
    pixels = width * height
    if kind == "av1":    # main profile, 8-bit; level 4.0 up to 2048x1088-ish, 5.0 up to 4K, 6.0 beyond
        level = 8 if pixels <= 2_228_224 else 12 if pixels <= 8_912_896 else 16
        return f"av01.0.{level:02d}M.08"
    if kind == "vp9":    # profile 0, 8-bit
        level = 40 if pixels <= 2_228_224 else 50 if pixels <= 8_912_896 else 60
        return f"vp09.00.{level}.08"
    if kind == "hevc":   # Main profile, main tier
        return f"hvc1.1.6.L{120 if pixels <= 2_228_224 else 150 if pixels <= 8_912_896 else 180}.B0"
    i = 0                # H.264: profile, constraints and level from the SPS
    while True:
        i = packet.find(b"\x00\x00\x01", i)
        if i < 0 or i + 6 > len(packet):
            return "avc1.42e01f"
        if packet[i + 3] & 0x1F == 7:
            profile, constraints, level = packet[i + 4], packet[i + 5], packet[i + 6]
            return f"avc1.{profile:02x}{constraints:02x}{level:02x}"
        i += 3


def pack_video(seq: int, size: tuple[int, int], data: bytes, key: bool, codec: str, at_ms: int = 0) -> bytes:
    """u8 2 | u32 seq | u16 width | u16 height | u8 flags (1 = keyframe) | u32 host time (ms, wraps) |
    [keyframe: u8 n, codec name] | bitstream

    The host time is when the picture was ready. The viewer uses it as live players do: it measures how unevenly
    pictures arrive (jitter) and shows them at the pace they were made, not in the bursts the network delivers."""
    head = struct.pack("<BIHHBI", 2, seq & 0xFFFFFFFF, size[0], size[1], 1 if key else 0, at_ms & 0xFFFFFFFF)
    if key:
        name = codec.encode("ascii")
        head += bytes([len(name)]) + name
    return head + data


def even(n: int) -> int:
    return max(2, n - n % 2)


def _open(encoder: str, kind: str, preset: str, width: int, height: int, fps: int, max_kbps: int):
    pix_fmt, opts = _options(encoder, kind, preset)
    ctx = av.CodecContext.create(encoder, "w")
    ctx.width, ctx.height, ctx.pix_fmt = width, height, pix_fmt
    ctx.time_base = Fraction(1, 1000)
    ctx.framerate = Fraction(fps, 1)
    for name, value in (("color_primaries", 1), ("color_trc", 1), ("colorspace", 1), ("color_range", 1)):
        try:                                   # BT.709, limited range: matches the colour conversion
            setattr(ctx, name, value)
        except (AttributeError, TypeError, ValueError):
            pass
    fam = _family(encoder)
    if fam in ("libx264", "nvenc", "qsv"):
        ctx.max_bit_rate = max_kbps * 1000
        ctx.rc_buffer_size = max_kbps * 1000 // max(1, fps)     # one picture's worth (game-streaming practice)
    elif fam == "videotoolbox":
        ctx.bit_rate = max_kbps * 1000
    elif encoder == "libsvtav1":
        opts["svtav1-params"] += f":mbr={max_kbps}"
    ctx.options = opts
    ctx.open()
    return ctx


class VideoEncoder:
    """One stream at a fixed size and preset. A new size or preset needs a new encoder (and a keyframe)."""

    def __init__(self, width: int, height: int, preset: str, fps: int, max_kbps: int, kind: str = "h264",
                 engine: str = "sw"):
        if av is None:
            raise RuntimeError("video encoding is not available (install PyAV)")
        encoder = encoder_for(kind, engine)
        if encoder is None:
            raise RuntimeError(f"no {engine} {kind} encoder here")
        self.kind, self.engine, self.encoder = kind, engine, encoder
        self.preset = preset if preset in PRESETS else "auto"
        self.width, self.height = even(width), even(height)
        self.fps = fps
        self.ctx = _open(encoder, kind, self.preset, self.width, self.height, fps, max_kbps)
        self.pix_fmt = self.ctx.pix_fmt
        self.max_kbps = max_kbps
        self.codec = ""
        self._pts = -1
        self._t0 = time.monotonic()
        self._span = 1 / max(1, fps)              # time covered by the current picture

    def set_max_kbps(self, kbps: int) -> None:
        """Change the bitrate cap now: libx264 applies it from the next picture without a keyframe. Other encoders
        keep the cap they were opened with (Cloudora adapts their size instead)."""
        kbps = max(150, int(kbps))
        if kbps != self.max_kbps and self.encoder == "libx264":
            self.max_kbps = kbps
            self.ctx.rc_buffer_size = 0                  # recompute both from the new cap
            self._fit_buffer(self._span)

    def _fit_buffer(self, span: float) -> None:
        """libx264: each picture may use the link for the time it actually covers. x264 (low-latency mode) assumes
        every picture lasts 1/fps, so a picture after 140 ms of a still screen would get 1/60 s worth of bits and
        come out blurry. Its refill rate and buffer (one picture) are scaled to the real interval instead: the
        average stays at the cap, with no delay. Changed only when it moves by a quarter (each is a reconfigure)."""
        span = min(0.1, max(1 / max(1, self.fps), span))     # at most 100 ms of the link: stays quick to send
        size = int(self.max_kbps * 1000 * span)
        if abs(size - self.ctx.rc_buffer_size) > 0.25 * self.ctx.rc_buffer_size:
            self.ctx.rc_buffer_size = size
            self.ctx.max_bit_rate = int(size * self.fps)

    def _picture(self, frame: Frame):
        vf = av.VideoFrame(frame.width, frame.height, "bgra")
        plane = vf.planes[0]
        rows = frame.height
        if plane.line_size == frame.stride and len(frame.data) >= frame.stride * rows:
            plane.update(memoryview(frame.data)[:frame.stride * rows])
        else:                                     # different row padding: copy row by row
            dst, src, n = memoryview(plane), memoryview(frame.data), frame.width * 4
            for y in range(rows):
                dst[y * plane.line_size:y * plane.line_size + n] = src[y * frame.stride:y * frame.stride + n]
        return vf.reformat(width=self.width, height=self.height, format=self.pix_fmt, dst_colorspace="itu709",
                           interpolation="BILINEAR")

    def encode(self, frame: Frame, key: bool = False) -> tuple[bytes, bool]:
        """Encode one picture; returns (bitstream bytes, is keyframe)."""
        pic = self._picture(frame)
        # Real timestamps (as OBS gives a broadcast encoder): the screen sends pictures only when it changes, so
        # the time between them varies. Rate control spends bits by time covered - a fixed step would tell it
        # that 7 pictures a second were 60, and it would starve each one of bits.
        previous = self._pts
        self._pts = pic.pts = max(self._pts + 1, int((time.monotonic() - self._t0) * 1000))
        if self.encoder == "libx264" and previous >= 0:
            self._span = (self._pts - previous) / 1000
            self._fit_buffer(self._span)
        if key:
            pic.pict_type = av.video.frame.PictureType.I
        data, is_key = b"", False
        for packet in self.ctx.encode(pic):
            data += bytes(packet)
            is_key = is_key or packet.is_keyframe
        if is_key:
            self.codec = codec_string(self.kind, data, self.width, self.height)
        return data, is_key

    def close(self) -> None:
        ctx, self.ctx = self.ctx, None
        if ctx is not None:
            try:
                ctx.encode(None)               # flush, so the encoder shuts down cleanly (SVT-AV1 insists)
            except Exception:  # noqa: BLE001
                pass                           # PyAV frees the encoder (and its GPU session) with its last reference
