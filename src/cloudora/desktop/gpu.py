"""Zero-copy GPU encoding on Linux (Wayland): the screen never leaves graphics memory.

    KDE/GNOME (PipeWire, DMA-BUF) -> GPU colour conversion + scaling (vapostproc) -> GPU encoder (VA-API)
    -> only the compressed picture comes to Python

Everything runs in GStreamer's native threads; Python receives a few kilobytes per picture, so encoding never holds
up the web server (no Python lock held while encoding). Measured on a laptop's integrated GPU with a real desktop:
~1.5 ms of CPU per picture *including* encoding (11 % of one core at 75 pictures/s), against 32-38 ms for software
encoding; AV1 at ~0.33 Mb/s and HEVC at ~0.23 Mb/s for scrolling/typing/video at ~37 dB.

Low-latency settings (the practice of game-streaming hosts such as Sunshine): no B-frames, one reference frame,
keyframes only on request (or after the encoder's maximum interval), constant quantiser with the
quality adjusted live when the link is congested, AV1 screen-content tools (palette mode, intra block copy), and a
one-picture leaky queue in front of the encoder so it always works on the newest screen, never a backlog.

Needs GStreamer's Python bindings (python3-gobject, installed with every GNOME/KDE desktop) and VA-API encoders
(Intel and AMD GPUs: gstreamer1-plugins-bad with the va plugin). Without them Cloudora uses PyAV (video.py).
"""

from __future__ import annotations

import os
import secrets
import sys
import sysconfig
import threading
import time
from typing import Callable

from cloudora.desktop import DesktopUnavailable

_GST = None

# kind -> (encoder element, parser + caps, quantiser)
ENCODERS = {
    "av1": ("vaav1enc", "av1parse ! video/x-av1,stream-format=obu-stream,alignment=tu", 110),
    "hevc": ("vah265enc", "h265parse config-interval=-1 ! video/x-h265,stream-format=byte-stream,alignment=au", 30),
    "h264": ("vah264enc", "h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au", 26),
}
GAME_KBPS = 8000          # mouse captured (games): a constant 8 Mb/s to start (Selkies' default), then follows the link
QP_STEP = {"av1": 15, "hevc": 3, "h264": 3}          # one notch of Auto (congestion) per codec
# VA-API encoders restart their stream (a keyframe) whenever quality or bitrate changes, so those change rarely
# (service.py). A still screen is re-sent this often, so that a change - e.g. full quality again once congestion is
# over, which works like Selkies' "paint-over" - also reaches a screen that is not changing.
KEEPALIVE_MS = 500
QP_MAX = {"av1": 200, "hevc": 42, "h264": 42}
START_SECONDS = 6


def _load():
    """GStreamer's Python bindings: Cloudora's own environment first, else the system's (same Python, same ABI)."""
    global _GST
    if _GST is not None:
        return _GST or None
    try:
        try:
            import gi
        except ImportError:
            base = sysconfig.get_paths(vars={"base": sys.base_prefix, "platbase": sys.base_prefix})
            found = [p for p in {base["platlib"], base["purelib"]} if os.path.isdir(os.path.join(p, "gi"))]
            if sys.base_prefix == sys.prefix or not found:
                raise
            sys.path.extend(found)
            try:
                import gi
            finally:
                for p in found:
                    sys.path.remove(p)
        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstApp, GstVideo
        assert GstApp                          # importing it gives appsink its pull methods
        Gst.init(None)
        _GST = (Gst, GstVideo)
    except Exception:  # noqa: BLE001 - no bindings: the PyAV path is used
        _GST = False
    return _GST or None


_kinds: list[str] | None = None


def available_kinds() -> list[str]:
    """Codecs the GPU can encode here, zero copy (cached)."""
    global _kinds
    if _kinds is None:
        loaded = _load()
        if not loaded or os.environ.get("CLOUDORA_NO_GPU"):
            _kinds = []
        else:
            Gst = loaded[0]
            need = ("pipewiresrc", "vapostproc", "appsink")
            _kinds = [k for k, (enc, parse, _) in ENCODERS.items()
                      if all(Gst.ElementFactory.find(e) for e in (*need, enc, parse.split()[0]))]
    return list(_kinds)


def codec_string(kind: str, caps: str, data: bytes, width: int, height: int) -> str:
    """WebCodecs codec name from the stream's own caps (profile/level), or from its size."""
    import re
    level = re.search(r"level=\(string\)([\d.]+)", caps)
    if kind == "av1":
        if level:
            major, _, minor = level.group(1).partition(".")
            return f"av01.0.{(int(major) - 2) * 4 + int(minor or 0):02d}M.08"
        return f"av01.0.{8 if width * height <= 2_228_224 else 12:02d}M.08"
    if kind == "hevc":
        lvl = int(round(float(level.group(1)) * 30)) if level else (120 if width * height <= 2_228_224 else 150)
        return f"hvc1.1.6.L{lvl}.B0"
    from cloudora.desktop.video import codec_string as h264_string
    return h264_string("h264", data, width, height)


class GstEncoder:
    """One GPU-encoded stream of the shared screen. read() returns each compressed picture as it is ready."""

    def __init__(self, pw_fd: int, node: int, kind: str, preset: str, fps: int,
                 link: Callable[[str], None] = lambda _name: None, source: str = ""):
        loaded = _load()
        if not loaded or kind not in ENCODERS:
            os.close(pw_fd) if pw_fd >= 0 else None
            raise DesktopUnavailable("GPU encoding is not available here.")
        self.Gst, self.GstVideo = loaded
        Gst = self.Gst
        self._fd = pw_fd                       # pipewiresrc makes its own copy; this one is closed in close()
        self.kind, self.preset, self.engine = kind, preset, "gpu"
        element, parse, self.base_qp = ENCODERS[kind]
        self.qp = self.base_qp
        self.name = "cloudora-gpu-" + secrets.token_hex(4)
        props = {"av1": f"rate-control=cqp qp={self.qp} palette-mode=true allow-intrabc=true",
                 "hevc": f"rate-control=cqp qpi={self.qp} qpp={self.qp} b-frames=0 aud=true",
                 "h264": f"rate-control=cqp qpi={self.qp} qpp={self.qp} b-frames=0 aud=true"}[kind]
        self.fps = max(1, fps)
        self.kbps = GAME_KBPS
        if preset == "game":
            # Games: a constant bitrate with a rate buffer of about one picture (game-streaming practice), so every
            # picture is about the same size - steady latency instead of quality-driven bursts on fast motion.
            props = props.replace("rate-control=cqp", f"rate-control=cbr bitrate={self.kbps} "
                                                      f"cpb-size={max(1, self.kbps // self.fps)}")
        src = source or (f"pipewiresrc fd={pw_fd} path={node} always-copy=false client-name={self.name} "
                         f"keepalive-time={KEEPALIVE_MS} stream-properties=props,node.name=(string){self.name}")
        # Latency: at most one raw picture waits before the encoder (older ones are dropped, never encoded) and at
        # most one encoded picture waits for Cloudora. Encoded pictures cannot be dropped (each builds on the
        # last), so a deeper output buffer would fill during a network stall and then never drain - every later
        # picture arriving that much older (measured: 4 buffers = pictures 28 ms old at steady 60 fps and ~90 ms
        # after a stall; 1 buffer = 4 ms). Fast screens (120/144 Hz) are held to the frame rate wanted by a probe
        # below (videorate cannot: screens report a variable frame rate).
        description = (f"{src} ! queue max-size-buffers=1 leaky=downstream ! vapostproc name=conv ! "
                       f"capsfilter name=size caps=video/x-raw(memory:VAMemory),format=NV12 ! "
                       f"{element} name=enc {props} ref-frames=1 key-int-max=1024 target-usage=6 ! {parse} ! "
                       f"appsink name=out sync=false max-buffers=1 drop=false emit-signals=false")
        try:
            self.pipe = Gst.parse_launch(description)
        except Exception as exc:  # noqa: BLE001
            raise DesktopUnavailable(f"GPU encoder could not start: {exc}") from exc
        # Scaling to the viewer changes only vapostproc's output. Its renegotiation request must not reach the
        # PipeWire source: that would make the desktop restart its stream (measured: the picture stops).
        def keep_source(_pad, info):
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.RECONFIGURE:
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK
        conv_sink = self.pipe.get_by_name("conv").get_static_pad("sink")
        self._probe = conv_sink.add_probe(Gst.PadProbeType.EVENT_UPSTREAM, keep_source)
        # At most `fps` pictures a second reach the encoder, dropped here before any GPU work: each picture has a
        # due time one interval after the last one's (a quarter interval early is fine, so pictures at the screen's
        # own pace always pass); on a faster screen (120/144 Hz) that averages exactly `fps`.
        interval = Gst.SECOND // self.fps
        early, due = interval // 4, [-1]

        def pace(_pad, info):
            pts = info.get_buffer().pts
            if pts == Gst.CLOCK_TIME_NONE:
                return Gst.PadProbeReturn.OK
            if due[0] >= 0 and pts < due[0] - early:
                return Gst.PadProbeReturn.DROP
            due[0] = due[0] + interval if due[0] >= 0 and pts - due[0] < interval else pts + interval
            return Gst.PadProbeReturn.OK
        conv_sink.add_probe(Gst.PadProbeType.BUFFER, pace)
        self.enc = self.pipe.get_by_name("enc")
        self.size_filter = self.pipe.get_by_name("size")
        self.out = self.pipe.get_by_name("out")
        self.bus = self.pipe.get_bus()
        self.width = self.height = 0
        self.native = (0, 0)
        self.codec = ""
        self._target = (0, 0)
        self._lock = threading.Lock()

        self._link = link
        if not source:
            # PipeWire's source waits inside "start" until its stream is linked, and some session managers never
            # link it (see wayland.py) - so the link is made from another thread while the pipeline starts.
            def link_soon() -> None:
                for delay in (0.3, 1.0, 2.0):
                    time.sleep(delay)
                    if self.pipe is None or self.width:
                        return
                    link(self.name)
            threading.Thread(target=link_soon, name="cloudora-gpu-link", daemon=True).start()
        # Starting waits until the desktop's stream is connected. Never wait forever: on a time-out Cloudora uses
        # another encoder instead (the stuck pipeline is abandoned on a background thread).
        result: list = []
        starter = threading.Thread(target=lambda: result.append(self.pipe.set_state(Gst.State.PLAYING)),
                                   name="cloudora-gpu-start", daemon=True)
        starter.start()
        starter.join(START_SECONDS)
        if not result or result[0] == Gst.StateChangeReturn.FAILURE:
            pipe, self.pipe = self.pipe, None
            threading.Thread(target=lambda: pipe.set_state(Gst.State.NULL), daemon=True).start()
            self.close()
            raise DesktopUnavailable("GPU encoder could not start.")

    def _check_bus(self) -> None:
        Gst = self.Gst
        while True:
            msg = self.bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is None:
                return
            if msg.type == Gst.MessageType.EOS:
                raise DesktopUnavailable("Screen sharing stopped on the computer.")
            err, _debug = msg.parse_error()
            raise DesktopUnavailable(f"GPU encoding stopped: {err.message}")

    def read(self, timeout: float) -> tuple[bytes, bool] | None:
        """The next compressed picture (bytes, is keyframe), or None if the screen did not change in time."""
        self._check_bus()
        sample = self.out.try_pull_sample(int(timeout * 1e9))
        if sample is None:
            return None
        buf = sample.get_buffer()
        ok, info = buf.map(self.Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        key = not buf.has_flags(self.Gst.BufferFlags.DELTA_UNIT)
        caps = sample.get_caps()
        if caps is not None:
            st = caps.get_structure(0)
            w, h = st.get_value("width") or 0, st.get_value("height") or 0
            if (w, h) != (self.width, self.height):
                self.width, self.height = w, h
            if key:
                self.codec = codec_string(self.kind, caps.to_string(), data, w, h)
        if not any(self.native):
            pad_caps = self.size_filter.get_static_pad("sink").get_current_caps()
            if pad_caps is not None:
                st = pad_caps.get_structure(0)
                self.native = (st.get_value("width") or 0, st.get_value("height") or 0)
        return data, key

    def request_key(self) -> None:
        """Ask the encoder for a keyframe now (a viewer connected, or its decoder needs one)."""
        ev = self.GstVideo.video_event_new_upstream_force_key_unit(self.Gst.CLOCK_TIME_NONE, True, 0)
        self.out.send_event(ev)

    def fit(self, max_w: int, max_h: int) -> None:
        """Scale on the GPU so pictures are no larger than the viewer shows them (no restart; a keyframe follows)."""
        nw, nh = self.native
        if not nw or not nh:
            return
        k = min(1.0, max_w / nw, max_h / nh)
        target = (max(2, int(nw * k) // 2 * 2), max(2, int(nh * k) // 2 * 2))
        if target == self._target:
            return
        self._target = target
        caps = "video/x-raw(memory:VAMemory),format=NV12" + (
            "" if target == (nw, nh) else f",width={target[0]},height={target[1]}")
        self.size_filter.set_property("caps", self.Gst.Caps.from_string(caps))

    def set_bitrate(self, kbps: int) -> None:
        """Mouse captured (games): change the constant bitrate now (no restart, no keyframe)."""
        kbps = max(500, int(kbps))
        if self.preset != "game" or kbps == self.kbps:
            return
        self.kbps = kbps
        self.enc.set_property("bitrate", kbps)
        self.enc.set_property("cpb-size", max(1, kbps // self.fps))

    def _apply_qp(self) -> None:
        qp = self.qp
        if self.kind == "av1":
            self.enc.set_property("qp", qp)
        else:
            self.enc.set_property("qpi", qp)
            self.enc.set_property("qpp", qp)

    def set_pressure(self, notches: int) -> None:
        """Auto: raise the quantiser by ``notches`` steps while the link is congested (0 = full quality)."""
        qp = min(QP_MAX[self.kind], self.base_qp + max(0, notches) * QP_STEP[self.kind])
        if qp == self.qp:
            return
        self.qp = qp
        self._apply_qp()

    def __del__(self) -> None:
        self.close()                       # never let a running pipeline be discarded (that can crash)

    def close(self) -> None:
        pipe = getattr(self, "pipe", None)
        if pipe is not None:
            try:
                pipe.set_state(self.Gst.State.NULL)
            except Exception:  # noqa: BLE001
                pass
            self.pipe = None
        fd, self._fd = getattr(self, "_fd", -1), -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
