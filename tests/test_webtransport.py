"""WebTransport (HTTP/3 over QUIC): the same viewer protocol as the WebSocket, with the same sign-in checks, plus
sound as datagrams. A real aioquic client plays the browser."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import struct
import time

import pytest

from cloudora import webtransport
from tests.conftest import free_port
from tests.test_desktop import server  # noqa: F401 - the portal fixture
from tests.test_desktop import wait_for

pytestmark = pytest.mark.skipif(not webtransport.available(), reason="needs aioquic")


@pytest.fixture
async def wt(env, server):  # noqa: F811
    gate = webtransport.WebTransportServer(env.app, server.portal)
    assert await gate.start("127.0.0.1", free_port())
    server.portal.webtransport = gate
    yield gate
    gate.close()


async def signed_in(env, server, wt, **kw):  # noqa: F811
    from tests.wt_client import wt_viewer
    _tok, session = env.sign_in()
    cm = wt_viewer("127.0.0.1", wt.port, **kw)
    v = await cm.__aenter__()
    assert await v.open(v.authority, v.origin) == 200
    v.send_json({"k": server.portal.screen_token(session)})
    v.cm = cm
    return v


async def test_pictures_and_input_over_webtransport(env, server, wt):  # noqa: F811
    v = await signed_in(env, server, wt)
    try:
        hello = await v.until(lambda m: isinstance(m, dict) and m.get("t") == "hello")
        assert hello["backend"] == "Fake screen"
        v.send_json({"t": "cfg", "w": 320, "h": 200, "q": 80, "auto": True, "fps": 30, "codecs": [], "p": "auto"})
        pic = await v.until(lambda m: isinstance(m, bytes) and m[0] == 1)       # a picture, on its own stream
        v.send_json({"t": "ack", "s": struct.unpack_from("<I", pic, 1)[0]})
        v.send_json({"t": "m", "x": 0.5, "y": 0.25})
        v.send_json({"t": "k", "c": "KeyA", "d": True})
        v.send_json({"t": "k", "c": "KeyA", "d": False})
        await wait_for(lambda: ("key", "KeyA", False) in env.screen.events)
        assert env.screen.events[:3] == [("move", 0, 0.5, 0.25), ("key", "KeyA", True), ("key", "KeyA", False)]
        assert env.app.desktop.viewer.ws.kind == "webtransport"
    finally:
        await v.cm.__aexit__(None, None, None)


async def test_sound_goes_as_datagrams(env, server, wt, monkeypatch):  # noqa: F811
    from cloudora.desktop import audio

    class FakeSound:
        def read(self, timeout):
            time.sleep(0.01)
            return b"opus-packet"

        def close(self):
            pass
    monkeypatch.setattr(audio, "available", lambda: True)
    monkeypatch.setattr(audio, "open_source", FakeSound)
    v = await signed_in(env, server, wt)
    try:
        await v.until(lambda m: isinstance(m, dict) and m.get("t") == "hello")
        v.send_json({"t": "cfg", "audio": True})
        m = await v.until(lambda m: isinstance(m, bytes) and m[0] == 3)
        assert m[5:] == b"opus-packet"
    finally:
        await v.cm.__aexit__(None, None, None)


async def test_wrong_token_is_sent_to_sign_in(env, server, wt):  # noqa: F811
    from tests.wt_client import wt_viewer
    async with wt_viewer("127.0.0.1", wt.port) as v:
        assert await v.open(v.authority, v.origin) == 200
        v.send_json({"k": "not-a-token"})
        m = await v.until(lambda m: isinstance(m, dict))
        assert m == {"t": "close", "code": 4401}
        assert env.opener.opened == 0                                # the screen was never opened


async def test_other_origins_are_refused(env, server, wt):  # noqa: F811
    from tests.wt_client import wt_viewer
    async with wt_viewer("127.0.0.1", wt.port, origin="https://evil.example") as v:
        assert await v.open(v.authority, v.origin) == 403


async def test_silent_connections_are_dropped(env, server, wt, monkeypatch):  # noqa: F811
    from tests.wt_client import wt_viewer
    monkeypatch.setattr(webtransport, "AUTH_SECONDS", 0.3)
    async with wt_viewer("127.0.0.1", wt.port) as v:
        assert await v.open(v.authority, v.origin) == 200
        with pytest.raises(ConnectionError):
            await v.next(timeout=3)


async def test_a_webtransport_viewer_takes_over_and_new_key_ends_it(env, server, wt):  # noqa: F811
    from tests.test_desktop import screen
    async with screen(env, server) as ws_viewer:
        v = await signed_in(env, server, wt)
        try:
            bye = await ws_viewer.until(lambda m: isinstance(m, dict) and m.get("t") == "bye")
            assert "took over" in bye["msg"]
            await v.until(lambda m: isinstance(m, dict) and m.get("t") == "hello")
            env.app.new_key()
            await v.until(lambda m: isinstance(m, dict) and m.get("t") == "bye")
        finally:
            await v.cm.__aexit__(None, None, None)


def test_certificate_is_what_browsers_accept_for_pinning():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    cert, key, digest = webtransport.make_certificate()
    assert isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1"
    life = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert life <= datetime.timedelta(days=14)
    assert digest == hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest()


def test_congestion_window_is_gentle_with_losses():
    from types import SimpleNamespace
    cc = webtransport.GentleCongestionControl(max_datagram_size=1200)
    start = cc.congestion_window
    pkt = lambda t: SimpleNamespace(sent_bytes=1200, sent_time=t)          # noqa: E731
    for _ in range(3):
        cc.on_packet_sent(packet=pkt(1.0))
    cc.on_packets_lost(now=2.0, packets=[pkt(1.0), pkt(1.0)])              # one episode: one gentle step
    assert cc.congestion_window == int(start * 0.85)
    cc.on_packets_lost(now=2.1, packets=[pkt(1.5)])                        # same episode: no further cut
    assert cc.congestion_window == int(start * 0.85)
    for i in range(40):
        cc.on_packets_lost(now=3.0 + i, packets=[pkt(2.5 + i)])
    assert cc.congestion_window == cc.FLOOR                                # never below the floor
    cc.on_packet_acked(now=50.0, packet=pkt(49.0))
    assert cc.congestion_window > cc.FLOOR
