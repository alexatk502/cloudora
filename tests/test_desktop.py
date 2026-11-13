"""The screen: picture encoding, the WebSocket (who may connect), flow control, input, and clean endings.

Runs a real uvicorn server in-process and talks to it with a real WebSocket client; the screen is tests/fake_desktop.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import random
import re
import struct
import time

import pytest
from PIL import Image
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from cloudora.desktop import Frame, WheelAccumulator, keys
from cloudora.desktop import service as service_mod
from cloudora.desktop.frames import ALIGN, Encoder, changed_rects, pack
from cloudora.portal.app import create_portal
from tests.conftest import free_port, make_env
from tests.fake_desktop import paint, solid


# ------------------------------------------------------------------ helpers
def decode(msg: bytes) -> tuple[int, tuple[int, int], list[tuple[int, int, int, int, Image.Image]]]:
    kind, seq, w, h, n = struct.unpack_from("<BIHHH", msg)
    assert kind == 1
    off, patches = 11, []
    for _ in range(n):
        x, y, pw, ph, ln = struct.unpack_from("<HHHHI", msg, off)
        off += 12
        img = Image.open(io.BytesIO(msg[off:off + ln]))
        assert img.format == "JPEG" and img.size == (pw, ph)
        patches.append((x, y, pw, ph, img.convert("RGB")))
        off += ln
    assert off == len(msg)
    return seq, (w, h), patches


def max_error(a: Image.Image, b: Image.Image) -> int:
    from PIL import ImageChops
    return max(hi for _lo, hi in ImageChops.difference(a, b).getextrema())


def as_image(buf: bytes | bytearray, w: int, h: int) -> Image.Image:
    return Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGRX", w * 4, 1)


class Screen:
    """A signed-in viewer's WebSocket, as the browser holds it."""

    def __init__(self, ws):
        self.ws = ws
        self.canvas: Image.Image | None = None
        self.info: list[dict] = []

    async def next(self, timeout: float = 3.0):
        msg = await asyncio.wait_for(self.ws.recv(), timeout)
        return json.loads(msg) if isinstance(msg, str) else msg

    async def until(self, pred, timeout: float = 5.0):
        async with asyncio.timeout(timeout):
            while True:
                m = await self.next(timeout)
                if isinstance(m, dict):
                    self.info.append(m)
                if pred(m):
                    return m

    async def picture(self, ack: bool = True, timeout: float = 5.0):
        m = await self.until(lambda m: isinstance(m, bytes), timeout)
        seq, size, patches = decode(m)
        if self.canvas is None or self.canvas.size != size:
            self.canvas = Image.new("RGB", size)
        for x, y, _w, _h, img in patches:
            self.canvas.paste(img, (x, y))
        if ack:
            await self.send({"t": "ack", "s": seq})
        return seq, size, patches

    async def send(self, obj) -> None:
        await self.ws.send(json.dumps(obj))


@pytest.fixture
async def server(env):
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield
    web = create_portal(env.app)
    port = free_port()
    srv = _Server(uvicorn.Config(web, host="127.0.0.1", port=port, log_level="warning",
                                 ws_per_message_deflate=False))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.01)
    srv.host_port, srv.portal = f"127.0.0.1:{port}", web.state.portal
    yield srv
    env.app.sessions.end_all("test over")
    await env.app.desktop.close()
    srv.should_exit = True
    await task


@contextlib.asynccontextmanager
async def screen(env, server, session=None, origin=None, token=None, hello=True):
    tok, s = session or env.sign_in()
    headers = {"Origin": origin or f"http://{server.host_port}", "Cookie": f"__Host-cloudora={tok}"}
    async with connect(f"ws://{server.host_port}/ws", additional_headers=headers, open_timeout=5) as ws:
        await ws.send(json.dumps({"k": token if token is not None else server.portal.screen_token(s)}))
        v = Screen(ws)
        if hello:
            v.hello = await v.until(lambda m: isinstance(m, dict) and m.get("t") == "hello")
        yield v


async def wait_for(pred, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.02)


# ------------------------------------------------------------------ encoding
def test_changed_rects_cover_every_change_and_align():
    rnd = random.Random(7)
    w, h = 300, 170
    cur = bytes(rnd.getrandbits(8) for _ in range(w * h * 3))
    for _ in range(40):
        prev = cur
        cur = bytearray(prev)
        for _ in range(rnd.randint(1, 4)):
            x, y = rnd.randrange(w), rnd.randrange(h)
            cur[(y * w + x) * 3] ^= 0xFF
        cur = bytes(cur)
        rects = changed_rects(prev, cur, w, h)
        assert rects
        for i in range(0, len(cur), 3):
            if cur[i:i + 3] != prev[i:i + 3]:
                px, py = (i // 3) % w, (i // 3) // w
                assert any(x <= px < x + rw and y <= py < y + rh for x, y, rw, rh in rects), (px, py)
        for x, y, rw, rh in rects:
            assert x % ALIGN == 0 and (x + rw) % ALIGN == 0 or x + rw == w
            assert 0 <= x < x + rw <= w and 0 <= y < y + rh <= h
    assert changed_rects(b"\0" * (w * h * 3), b"\0" * (w * h * 3), w, h) == []


def test_encoder_sends_whole_screen_first_then_only_changes():
    w, h = 320, 200
    buf = solid(w, h, (40, 30, 20))
    enc = Encoder()
    size, patches = enc.encode(Frame(w, h, bytes(buf)), 4000, 4000, 90)
    assert size == (w, h) and [p[:4] for p in patches] == [(0, 0, w, h)]
    assert enc.encode(Frame(w, h, bytes(buf)), 4000, 4000, 90) is None          # nothing changed: nothing sent
    paint(buf, w, 100, 50, 10, 10, (0, 0, 255))
    size, patches = enc.encode(Frame(w, h, bytes(buf)), 4000, 4000, 90)
    assert len(patches) == 1
    x, y, pw, ph, data = patches[0]
    assert x <= 100 and y <= 50 and x + pw >= 110 and y + ph >= 60 and pw * ph < w * h / 10
    assert len(pack(1, size, patches)) < 3000


def test_encoder_scales_to_the_viewer_and_refreshes_on_resize():
    w, h = 320, 200
    enc = Encoder()
    size, patches = enc.encode(Frame(w, h, bytes(solid(w, h, (1, 2, 3)))), 160, 160, 70)
    assert size == (160, 100) and patches[0][:4] == (0, 0, 160, 100)
    size, patches = enc.encode(Frame(w, h, bytes(solid(w, h, (1, 2, 3)))), 320, 320, 70)
    assert size == (320, 200) and patches[0][:4] == (0, 0, 320, 200)      # new size: whole picture again


def test_key_table_is_consistent():
    evdev = [k.evdev for c, k in keys.KEYS.items() if not c.startswith("OS")]
    assert len(evdev) == len(set(evdev))
    for code in ("KeyA", "Digit1", "Enter", "ShiftLeft", "ControlLeft", "AltLeft", "MetaLeft", "ArrowUp", "F12",
                 "Backspace", "Tab", "Escape", "Space", "Delete"):
        k = keys.KEYS[code]
        assert k.evdev and k.win and k.mac is not None, code
    assert keys.KEYS["KeyA"].evdev == 30 and keys.KEYS["KeyA"].win == 0x1E and keys.KEYS["KeyA"].mac == 0
    assert keys.KEYS["ArrowLeft"].win == 0xE04B                    # extended scancode on Windows
    acc = WheelAccumulator()
    assert acc.add(60, 60) == (0, 0) and acc.add(60, 60) == (1, 1) and acc.add(-240, 0) == (-2, 0)


# ------------------------------------------------------------------ who may connect
async def test_screen_requires_sign_in_origin_and_token(env, server):
    tok, s = env.sign_in()
    good = server.portal.screen_token(s)
    with pytest.raises(InvalidStatus):                                       # not signed in
        async with connect(f"ws://{server.host_port}/ws", additional_headers={"Origin": f"http://{server.host_port}"}):
            pass
    with pytest.raises(InvalidStatus):                                       # another website's page
        async with screen(env, server, (tok, s), origin="https://evil.example"):
            pass
    async with screen(env, server, (tok, s), token="forged", hello=False) as v:   # page from before a restart
        with pytest.raises(ConnectionClosed):
            await v.next()
    other_tok, other = env.sign_in()
    async with screen(env, server, (other_tok, other), token=good, hello=False) as v:  # token of another sign-in
        with pytest.raises(ConnectionClosed):
            await v.next()
    assert env.opener.opened == 0                                            # the screen was never opened


# ------------------------------------------------------------------ pictures
async def test_pictures_arrive_whole_then_as_changes(env, server):
    async with screen(env, server) as v:
        assert v.hello["backend"] == "Fake screen"
        await v.send({"t": "cfg", "w": 4000, "h": 4000, "q": 92, "auto": False, "fps": 60})
        await v.picture()
        await v.send({"t": "ref"})
        _, size, patches = await v.picture()
        assert size == (320, 200) and patches[0][:4] == (0, 0, 320, 200)
        env.screen.change(200, 120, 20, 12)
        _, _, patches = await v.picture()
        assert sum(p[2] * p[3] for p in patches) < 320 * 200 / 8           # only the changed area travelled
        await wait_for(lambda: True)
        assert max_error(v.canvas, as_image(env.screen.picture, 320, 200)) < 40   # JPEG: close, not exact


async def test_flow_control_never_queues_stale_pictures(env, server, monkeypatch):
    async with screen(env, server) as v:
        await v.send({"t": "cfg", "w": 4000, "h": 4000, "q": 50, "auto": False, "fps": 60})
        stop = asyncio.Event()

        async def keep_changing():
            i = 0
            while not stop.is_set():
                env.screen.change((i * 16) % 300, 0, 16, 16, (i % 255, 0, 0))
                i += 1
                await asyncio.sleep(0.005)
        changer = asyncio.create_task(keep_changing())
        try:
            for _ in range(10):                            # a healthy link: Cloudora learns its speed
                await v.picture()
            unacked = []
            with contextlib.suppress(TimeoutError):
                while True:                                # the viewer stops acknowledging (a stalled link)
                    seq, _, _ = await v.picture(ack=False, timeout=0.8)
                    unacked.append(seq)
            assert 1 <= len(unacked) <= 6                  # a few pictures in flight, then Cloudora waits
            for seq in unacked:
                await v.send({"t": "ack", "s": seq})
            for _ in range(5):                             # acknowledged: pictures flow again
                await v.picture()
        finally:
            stop.set()
            await changer


def test_flow_base_delay_is_not_inflated_by_its_own_queue():
    from cloudora.desktop.flow import Flow
    f, now = Flow(), 100.0
    f.sent(1, 10_000, now)                       # sent alone: a clean sample of the link
    f.sent(2, 50_000, now)                       # from here on the link is never idle
    f.acked(1, now + 0.02)
    assert f.base_rtt == pytest.approx(0.02)
    seq = 2
    for _ in range(15):                          # 6 s of a busy link: every picture waits behind another
        seq += 1
        f.sent(seq, 50_000, now)
        now += 0.4
        f.acked(seq - 1, now)
    assert f.base_rtt == pytest.approx(0.02)     # queued round trips never count as the base delay
    assert f.queue_delay() > 0.2
    assert not f.may_send(30, now)               # busy without a clean sample for 2 s: let the link drain
    f.acked(seq, now + 0.05)
    assert f.in_flight == {} and f.may_send(30, now + 0.05)


def test_flow_sends_one_picture_until_the_link_is_known_and_recovers_from_lost_acks():
    from cloudora.desktop.flow import Flow
    f = Flow()
    assert f.may_send(30, 0.0)
    f.sent(1, 1000, 0.0)
    assert not f.may_send(30, 0.01)              # unknown link: one at a time
    assert f.may_send(30, 3.5)                   # an acknowledgement 3 s late is lost: start again
    assert f.in_flight == {}


def test_wayland_input_messages_are_well_formed(monkeypatch, tmp_path):
    """Input on Wayland goes out as fire-and-forget D-Bus calls (no reply). Built without a desktop: a stand-in
    connection records the messages."""
    import threading

    from jeepney.low_level import MessageFlag

    from cloudora.desktop import wayland
    from cloudora.desktop.keys import KEYS

    sent = []

    class Conn:
        def send(self, msg):
            sent.append(msg)
    b = object.__new__(wayland.WaylandBackend)
    from jeepney import DBusAddress
    b.rd = DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop",
                       interface="org.freedesktop.portal.RemoteDesktop")
    b.conn, b._lock, b.session, b.node, b.width, b.height = Conn(), threading.Lock(), "/s/1", 84, 1536, 960
    b._wheel = wayland.WheelAccumulator()
    b.move(0, 0.5, 0.25)
    b.button(0, True)
    b.key(KEYS["KeyA"], True)
    b.wheel(0, 120)
    methods = [m.header.fields[wayland_member()] for m in sent]
    assert methods == ["NotifyPointerMotionAbsolute", "NotifyPointerButton", "NotifyKeyboardKeycode",
                       "NotifyPointerAxisDiscrete"]
    assert all(m.header.flags & MessageFlag.no_reply_expected for m in sent)
    assert sent[0].body == ("/s/1", {}, 84, 768.0, 240.0)
    assert sent[1].body[2:] == (0x110, 1) and sent[2].body[2:] == (30, 1) and sent[3].body[2:] == (0, 1)


def wayland_member():
    from jeepney.low_level import HeaderFields
    return HeaderFields.member


async def test_cursor_position_is_sent_when_not_in_the_picture(env, server):
    async with screen(env, server) as v:
        assert v.hello["cursor"] is True and v.hello["repeat"] is False
        m = await v.until(lambda m: isinstance(m, dict) and m.get("t") == "cur")
        assert (m["x"], m["y"]) == (0.25, 0.5)
        env.screen.cursor_pos = None
        env.screen.change(0, 0, 4, 4)
        await v.until(lambda m: isinstance(m, dict) and m == {"t": "cur"})


# ------------------------------------------------------------------ input
async def test_input_reaches_the_screen(env, server):
    async with screen(env, server) as v:
        await v.picture()
        for msg in ({"t": "m", "x": 0.5, "y": 0.25},
                    {"t": "b", "b": 0, "d": True, "x": 0.5, "y": 0.25}, {"t": "b", "b": 0, "d": False},
                    {"t": "b", "b": 2, "d": True}, {"t": "b", "b": 2, "d": False},
                    {"t": "w", "dx": 0, "dy": 240},
                    {"t": "k", "c": "ShiftLeft", "d": True}, {"t": "k", "c": "KeyA", "d": True},
                    {"t": "k", "c": "KeyA", "d": False}, {"t": "k", "c": "ShiftLeft", "d": False}):
            await v.send(msg)
        await wait_for(lambda: ("key", "ShiftLeft", False) in env.screen.events)
    assert env.screen.events == [
        ("move", 0, 0.5, 0.25), ("move", 0, 0.5, 0.25), ("button", 0, True), ("button", 0, False),
        ("button", 2, True), ("button", 2, False), ("wheel", 0, 240), ("key", "ShiftLeft", True),
        ("key", "KeyA", True), ("key", "KeyA", False), ("key", "ShiftLeft", False)]


async def test_bad_input_is_ignored(env, server):
    async with screen(env, server) as v:
        await v.picture()
        for msg in ({"t": "m", "x": "a", "y": 1}, {"t": "m", "x": float("nan")}, {"t": "b", "b": 7, "d": True},
                    {"t": "b", "b": 0, "d": "yes"}, {"t": "k", "c": "NoSuchKey", "d": True}, {"t": "k", "c": 5},
                    {"t": "w", "dx": True, "dy": 1}, {"t": "txt", "s": "typed text is not accepted"},
                    {"t": "nope"}, [1, 2], "junk"):
            await v.ws.send(msg if isinstance(msg, str) else json.dumps(msg))
        await v.send({"t": "m", "x": 5, "y": -3})                       # clamped onto the screen
        await wait_for(lambda: env.screen.events)
    assert env.screen.events == [("move", 0, 1.0, 0.0)]


async def test_held_keys_and_buttons_are_released_when_the_viewer_leaves(env, server):
    async with screen(env, server) as v:
        await v.picture()
        await v.send({"t": "k", "c": "ControlLeft", "d": True})
        await v.send({"t": "b", "b": 0, "d": True})
        await wait_for(lambda: len(env.screen.events) == 2)
    await wait_for(lambda: len(env.screen.events) == 4)
    assert set(env.screen.events[2:]) == {("key", "ControlLeft", False), ("button", 0, False)}


async def test_view_only_ignores_input(tmp_path):
    e = make_env(tmp_path, view_only=True)
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield
    web = create_portal(e.app)
    port = free_port()
    srv = _Server(uvicorn.Config(web, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.01)
    srv.host_port, srv.portal = f"127.0.0.1:{port}", web.state.portal
    try:
        async with screen(e, srv) as v:
            assert v.hello["view_only"] is True
            await v.picture()
            await v.send({"t": "k", "c": "KeyA", "d": True})
            await v.send({"t": "m", "x": 0.1, "y": 0.1})
            await asyncio.sleep(0.3)
        assert e.screen.events == []
    finally:
        await e.app.desktop.close()
        srv.should_exit = True
        await task


# ------------------------------------------------------------------ endings
async def test_a_new_viewer_takes_over(env, server):
    async with screen(env, server) as first:
        await first.picture()
        async with screen(env, server) as second:
            bye = await first.until(lambda m: isinstance(m, dict) and m.get("t") == "bye")
            assert "took over" in bye["msg"]
            await second.picture()
    assert env.opener.opened == 1                      # the screen stayed open across the hand-over


async def test_signing_out_or_a_new_key_closes_the_screen(env, server):
    tok, s = env.sign_in()
    async with screen(env, server, (tok, s)) as v:
        await v.picture()
        env.app.new_key()
        bye = await v.until(lambda m: isinstance(m, dict) and m.get("t") == "bye")
        assert "signed out" in bye["msg"]
        with pytest.raises(ConnectionClosed):
            await v.next()


async def test_unavailable_screen_is_explained(tmp_path):
    e = make_env(tmp_path, error="Nobody allowed remote control on the computer in time.")
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield
    web = create_portal(e.app)
    port = free_port()
    srv = _Server(uvicorn.Config(web, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.01)
    srv.host_port, srv.portal = f"127.0.0.1:{port}", web.state.portal
    try:
        async with screen(e, srv, hello=False) as v:
            err = await v.until(lambda m: isinstance(m, dict) and m.get("t") == "err")
            assert "allowed" in err["msg"]
    finally:
        await e.app.desktop.close()
        srv.should_exit = True
        await task


async def test_screen_recovers_when_the_capture_fails_briefly(env, server):
    async with screen(env, server) as v:
        await v.picture()
        env.screen.fail_grabs = 1
        env.screen.change(0, 0, 32, 32)
        info = await v.until(lambda m: isinstance(m, dict) and m.get("t") == "info")
        assert "Reconnecting" in info["msg"]
        _, size, patches = await v.picture()
        assert patches[0][:4] == (0, 0, 320, 200)        # whole picture after reconnecting


async def test_screen_closes_after_the_last_viewer_leaves(env, server, monkeypatch):
    monkeypatch.setattr(service_mod, "IDLE_CLOSE_SECONDS", 0.2)
    async with screen(env, server) as v:
        await v.picture()
    await wait_for(lambda: env.screen.closed and env.app.desktop.backend is None)


async def test_stopping_while_the_screen_waits_for_permission(env, server):
    """Wayland's first run waits (up to minutes) for someone to allow remote control. Stopping Cloudora, or the viewer
    being signed out, must not wait for that."""
    from cloudora.desktop import DesktopUnavailable

    def waits_for_permission(data_dir, status=lambda _m: None, cancel=None):
        status("Waiting for someone at the computer to allow remote control (only needed once).")
        if not cancel.wait(30):
            raise AssertionError("never cancelled")
        raise DesktopUnavailable("Cloudora is stopping.")
    env.app.desktop.opener = waits_for_permission
    async with screen(env, server, hello=False) as v:
        await v.until(lambda m: isinstance(m, dict) and "allow remote control" in m.get("msg", ""))
        started = asyncio.get_running_loop().time()
        await env.app.desktop.close()
        assert asyncio.get_running_loop().time() - started < 3
        bye = await v.until(lambda m: isinstance(m, dict) and m.get("t") == "bye")
        assert "stopping" in bye["msg"]


# ------------------------------------------------------------------ H.264
def decode_video(msg: bytes):
    kind, seq, w, h, flags, _at = struct.unpack_from("<BIHHBI", msg)
    assert kind == 2
    off, codec = 14, ""
    if flags & 1:
        n = msg[off]
        codec = msg[off + 1:off + 1 + n].decode()
        off += 1 + n
    return seq, (w, h), bool(flags & 1), codec, msg[off:]


DECODERS = {"av1": "libdav1d", "vp9": "vp9", "h264": "h264", "hevc": "hevc"}
NAMES = {"av1": r"av01\.0\.\d\dM\.08", "vp9": r"vp09\.00\.\d\d\.08", "h264": r"avc1\.[0-9a-f]{6}",
         "hevc": r"hvc1\.1\.6\.L\d+\.B0"}
COMBOS = [("av1", "sw"), ("vp9", "sw"), ("h264", "sw"), ("av1", "hw"), ("hevc", "hw"), ("h264", "hw")]


def need(kind, engine):
    from cloudora.desktop import video
    if video.encoder_for(kind, engine) is None:
        pytest.skip(f"no {engine} {kind} encoder on this computer")


def decoder_for(kind):
    import av
    dec = av.CodecContext.create(DECODERS[kind], "r")
    if kind == "av1":
        dec.options = {"max_frame_delay": "1"}             # like the browser with optimizeForLatency
    return dec


@pytest.mark.parametrize("kind,engine", COMBOS)
async def test_video_stream_is_decodable_and_cheap(env, server, kind, engine, monkeypatch):
    need(kind, engine)
    import av
    monkeypatch.setattr(env.app.desktop, "encoders", lambda backend: [(kind, engine)])
    dec = decoder_for(kind)

    def frames_of(data):                                   # each chunk is one whole picture, as for WebCodecs
        return dec.decode(av.Packet(data))
    async with screen(env, server) as v:
        assert v.hello["video"] is True and v.hello["codecs"] == [kind]
        await v.send({"t": "cfg", "w": 4000, "h": 4000, "q": 80, "auto": True, "fps": 60, "codecs": [kind], "p": "auto"})
        m = await v.until(lambda m: isinstance(m, bytes) and m[0] == 2)
        seq, size, key, codec, data = decode_video(m)
        assert key and re.fullmatch(NAMES[kind], codec) and size == (320, 200)
        pic = frames_of(data)[0].to_image()
        assert max_error(pic, as_image(env.screen.picture, 320, 200)) < 60
        await v.send({"t": "ack", "s": seq})
        with contextlib.suppress(TimeoutError):             # the still picture is sharpened a few times, then quiet
            while True:
                m = await v.until(lambda m: isinstance(m, bytes), timeout=0.6)
                s2, _, k2, _, d2 = decode_video(m)
                assert not k2
                frames_of(d2)
                await v.send({"t": "ack", "s": s2})
        env.screen.change(100, 60, 24, 24, (0, 0, 255))     # a small change: a small delta picture
        m = await v.until(lambda m: isinstance(m, bytes))
        s3, _, k3, _, d3 = decode_video(m)
        assert not k3 and len(d3) < 3000                     # a 24x24 change costs little
        pic = frames_of(d3)[-1].to_image()
        r, g, b = pic.getpixel((112, 72))
        assert r > 180 and g < 90 and b < 90               # the red square arrived
        await v.send({"t": "ack", "s": s3})
        await v.send({"t": "ref"})                          # e.g. the viewer's decoder failed: a new keyframe
        m = await v.until(lambda m: isinstance(m, bytes) and decode_video(m)[2])
        await v.send({"t": "ack", "s": decode_video(m)[0]})


@pytest.mark.parametrize("kind,engine", COMBOS)
def test_video_encoder_handles_odd_sizes_and_padded_rows(kind, engine):
    need(kind, engine)
    from cloudora.desktop.video import VideoEncoder
    enc = VideoEncoder(1365, 767, "auto", 30, 5000, kind=kind, engine=engine)
    assert (enc.width, enc.height) == (1364, 766)
    data, key = enc.encode(Frame(1365, 767, bytes(1368 * 4 * 767), 1368 * 4), key=True)
    assert key and data and re.fullmatch(NAMES[kind], enc.codec)
    if (kind, engine) == ("h264", "sw"):
        assert data.startswith(b"\x00\x00\x00\x01")
        enc.set_max_kbps(10)                                # clamped, never zero
        assert enc.max_kbps == 150
    enc.close()


def test_encoder_plan_prefers_the_gpu_and_keeps_software_fallbacks(monkeypatch):
    from cloudora.desktop import video
    monkeypatch.setattr(video, "encoder_for", lambda kind, engine: engine == "sw" and kind in ("h264", "vp9", "av1")
                        or engine == "hw" and kind == "hevc")
    plan = video.plan(["av1"])
    assert plan == [("av1", "gpu"), ("hevc", "hw"), ("h264", "sw"), ("vp9", "sw"), ("av1", "sw")]
    assert video.choose(plan, ["h264", "av1", "vp9"]) == ("av1", "gpu")        # the best both sides can do
    assert video.choose(plan, ["h264"]) == ("h264", "sw")
    assert video.plan(["av1", "hevc", "h264"])[:4] == [("av1", "gpu"), ("hevc", "gpu"), ("h264", "sw"), ("h264", "gpu")]
    assert video.choose([o for o in plan if o[1] != "gpu"], ["av1"]) == ("av1", "sw")   # after a GPU failure
    assert video.choose(plan, []) == video.choose(plan, "junk") == video.choose(plan, None) == ("jpeg", "")


# ------------------------------------------------------------------ zero-copy GPU encoding (Linux, VA-API)
def gpu_or_skip(kind):
    from cloudora.desktop import gpu
    if kind not in gpu.available_kinds():
        pytest.skip(f"no zero-copy GPU {kind} encoder here")
    return gpu


TEST_SOURCE = ("videotestsrc pattern=ball is-live=true ! "
               "video/x-raw,width=1280,height=800,framerate=60/1")


@pytest.mark.parametrize("kind", ["av1", "hevc", "h264"])
def test_gpu_encoder_keyframes_resizing_and_quality(kind):
    gpu = gpu_or_skip(kind)
    import av
    enc = gpu.GstEncoder(-1, 0, kind, "auto", 60, source=TEST_SOURCE)
    dec = decoder_for(kind)
    try:
        got = []
        deadline = time.monotonic() + 5
        while len(got) < 20 and time.monotonic() < deadline:
            r = enc.read(0.2)
            if r:
                got.append(r)
        assert len(got) == 20 and got[0][1] and not any(k for _, k in got[1:])   # one keyframe, then deltas only
        assert re.fullmatch(NAMES[kind], enc.codec) and (enc.width, enc.height) == (1280, 800)
        pictures = [f for data, _ in got for f in dec.decode(av.Packet(data))]
        assert pictures and pictures[0].width == 1280                 # what a browser would decode
        enc.request_key()                                              # a viewer's decoder needs a keyframe
        assert any(k for _, k in (enc.read(0.5) or (b"", False) for _ in range(10)))
        enc.fit(640, 640)                                              # a small viewer: scaled on the GPU
        sizes = set()
        for _ in range(20):
            r = enc.read(0.2)
            if r:
                sizes.add((enc.width, enc.height))
        assert (640, 400) in sizes
        enc.set_pressure(2)                                            # congestion: lower quality, live
        assert enc.qp > enc.base_qp
        enc.set_pressure(0)
        assert enc.qp == enc.base_qp
    finally:
        enc.close()


async def test_service_uses_the_zero_copy_gpu_encoder(env, server, monkeypatch):
    gpu = gpu_or_skip("av1")
    import av
    monkeypatch.setattr(env.screen, "gpu_codecs", lambda: ["av1"], raising=False)
    monkeypatch.setattr(env.screen, "open_gpu_encoder",
                        lambda kind, preset, fps: gpu.GstEncoder(-1, 0, kind, preset, fps, source=TEST_SOURCE),
                        raising=False)
    monkeypatch.setattr(env.app.desktop, "encoders", lambda backend: [("av1", "gpu"), ("h264", "sw")])
    async with screen(env, server) as v:
        await v.send({"t": "cfg", "w": 4000, "h": 4000, "q": 80, "auto": True, "fps": 60, "codecs": ["av1", "h264"],
                      "p": "auto"})
        m = await v.until(lambda m: isinstance(m, bytes) and m[0] == 2)
        seq, size, key, codec, data = decode_video(m)
        assert key and codec.startswith("av01") and size == (1280, 800)
        assert decoder_for("av1").decode(av.Packet(data))
        await v.send({"t": "ack", "s": seq})
        pictures, started = 0, time.monotonic()
        while time.monotonic() - started < 1.5:            # a viewer acknowledging every picture, as the page does
            m = await v.next()
            if isinstance(m, bytes) and m[0] == 2:
                await v.send({"t": "ack", "s": decode_video(m)[0]})
                pictures += 1
        assert pictures / 1.5 > 20
        assert env.app.desktop.viewer.gpu is not None and env.app.desktop.viewer.settings.engine == "gpu"


# ------------------------------------------------------------------ games: relative mouse, sound
async def test_relative_mouse_motion_reaches_the_screen(env, server):
    async with screen(env, server) as v:
        await v.picture()
        for msg in ({"t": "r", "dx": 15, "dy": -4}, {"t": "r", "dx": 99999, "dy": 0}, {"t": "r", "dx": 0, "dy": 0},
                    {"t": "r", "dx": 1.5, "dy": 2}, {"t": "r", "dx": True, "dy": 1}):
            await v.send(msg)
        await v.send({"t": "b", "b": 0, "d": True})            # a click while the pointer is locked: no position
        await v.send({"t": "b", "b": 0, "d": False})
        await wait_for(lambda: ("button", 0, False) in env.screen.events)
    assert env.screen.events[:4] == [("nudge", 15, -4), ("nudge", 4000, 0), ("button", 0, True), ("button", 0, False)]


async def test_sound_is_sent_only_while_turned_on(env, server, monkeypatch):
    from cloudora.desktop import audio

    class FakeSound:
        n = 0

        def read(self, timeout):
            time.sleep(0.01)
            FakeSound.n += 1
            return b"opus" + bytes([FakeSound.n % 256])

        def close(self):
            pass
    monkeypatch.setattr(audio, "available", lambda: True)
    monkeypatch.setattr(audio, "open_source", FakeSound)
    async with screen(env, server) as v:
        assert v.hello["audio"] is True
        await v.picture()
        await v.send({"t": "cfg", "audio": True})
        m = await v.until(lambda m: isinstance(m, bytes) and m[0] == 3)
        assert m[5:9] == b"opus" and struct.unpack_from("<I", m, 1)[0] >= 1
        await v.send({"t": "cfg", "audio": False})
        await asyncio.sleep(0.2)                                 # packets already on their way may still arrive
        with pytest.raises(TimeoutError):
            await v.until(lambda m: isinstance(m, bytes) and m[0] == 3, timeout=0.5)


def test_gpu_game_mode_constant_bitrate_changes_live():
    gpu = gpu_or_skip("av1")
    game = gpu.GstEncoder(-1, 0, "av1", "game", 60, source=TEST_SOURCE)
    try:
        assert game.enc.get_property("rate-control").value_nick == "cbr" and game.kbps == gpu.GAME_KBPS
        for _ in range(5):
            game.read(0.2)
        game.set_bitrate(3000)                                   # congestion control, live
        assert game.enc.get_property("bitrate") == 3000 and game.read(0.5)
    finally:
        game.close()


def test_game_mode_resolution_ladder_follows_the_bitrate():
    """Like a live platform's ladder: enough bits per pixel at every bitrate - smaller pictures when it falls,
    the full size again (with a margin, so it does not flip) when it recovers."""
    v = service_mod.Viewer.__new__(service_mod.Viewer)
    v.gpu, v.scale = None, 1.0
    s = service_mod.Settings()
    s.max_w, s.max_h, s.fps, s.codec, s.auto = 1920, 1080, 60, "av1", True
    full = 0.03 * 1920 * 1080 * 60 / 1000                       # ~3.7 Mb/s keeps 1080p60 AV1 sharp
    assert v._rung(int(full * 1.1), s) == 1.0
    v.scale = v._rung(int(full * 0.8), s)
    assert v.scale == 0.75
    assert v._rung(int(full * 1.1), s) == 0.75                   # not quite enough to go back up (30 % margin)
    assert v._rung(int(full * 1.4), s) == 1.0
    assert v._rung(1500, s) == 0.5 and v._rung(100, s) == 0.5    # never below the last step
    s.auto = False
    assert v._rung(100, s) == v.scale                            # a fixed size stays fixed


def test_video_timestamps_are_real_time():
    """Software encoders get the time each picture was made (variable frame rate), not a fixed step: a picture
    after a pause is allowed the bits of the time it covers."""
    video = pytest.importorskip("cloudora.desktop.video")
    if video.encoder_for("h264", "sw") is None:
        pytest.skip("no libx264")
    enc = video.VideoEncoder(64, 64, "auto", 60, 1000)
    try:
        frame = Frame(64, 64, bytes(64 * 64 * 4))
        enc.encode(frame, key=True)
        first = enc._pts
        time.sleep(0.1)
        enc.encode(frame)
        assert 90 <= enc._pts - first <= 400
        assert enc.ctx.rc_buffer_size == pytest.approx(1000 * 1000 * 0.1, rel=0.05)   # 100 ms of the cap: sharp
        enc.encode(frame)
        assert enc._pts > first + 90                              # always increasing, even within a millisecond
    finally:
        enc.close()
    packet = video.pack_video(7, (64, 64), b"x", True, "avc1.42001f", at_ms=123456)
    assert struct.unpack_from("<BIHHBI", packet) == (2, 7, 64, 64, 1, 123456)
