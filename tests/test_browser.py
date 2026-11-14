"""The viewer in a real browser: headless Firefox, driven over WebDriver BiDi, against Cloudora over HTTPS with the
fake screen. Signs in through the real sign-in script, checks the picture on the canvas, and sends real mouse and
keyboard events through the page. Skipped when Firefox is not installed."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import os
import shutil
import subprocess
import sys

import pytest

from cloudora.config import Config
from cloudora.portal.app import create_portal
from cloudora.tls import ensure_certificate, ssl_context
from tests.conftest import free_port

FIREFOX = shutil.which("firefox")
pytestmark = pytest.mark.skipif(not FIREFOX or sys.platform == "win32", reason="needs Firefox")


class Bidi:
    def __init__(self, ws):
        self.ws, self.ids = ws, itertools.count(1)

    async def cmd(self, method: str, **params):
        n = next(self.ids)
        await self.ws.send(json.dumps({"id": n, "method": method, "params": params}))
        while True:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if m.get("id") == n:
                if m.get("type") == "error":
                    raise AssertionError(m)
                return m["result"]

    async def js(self, ctx: str, expression: str):
        r = await self.cmd("script.evaluate", expression=expression, target={"context": ctx}, awaitPromise=True)
        assert r["type"] == "success", r
        return r["result"].get("value")

    async def until(self, ctx: str, expression: str, timeout: float = 15):
        async with asyncio.timeout(timeout):
            while True:
                try:
                    if await self.js(ctx, expression):
                        return
                except AssertionError:
                    pass          # navigating
                await asyncio.sleep(0.1)


@contextlib.asynccontextmanager
async def firefox(tmp_path):
    port = free_port()
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "user.js").write_text('user_pref("browser.shell.checkDefaultBrowser", false);\n'
                                     'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n')
    proc = subprocess.Popen([FIREFOX, "--headless", "--no-remote", "--profile", str(profile),
                             "--remote-debugging-port", str(port), "--width", "1200", "--height", "800"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ, "MOZ_CRASHREPORTER_DISABLE": "1"})
    from websockets.asyncio.client import connect
    try:
        for _ in range(150):
            try:
                ws = await connect(f"ws://127.0.0.1:{port}/session", max_size=None)
                break
            except OSError:
                await asyncio.sleep(0.2)
        else:
            raise AssertionError("Firefox did not start its remote protocol")
        async with ws:
            b = Bidi(ws)
            await b.cmd("session.new", capabilities={"alwaysMatch": {"acceptInsecureCerts": True}})
            tree = await b.cmd("browsingContext.getTree")
            yield b, tree["contexts"][0]["context"]
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.parametrize("transport", ["webtransport", "websocket"])
async def test_viewer_in_firefox(env, tmp_path, monkeypatch, transport):
    """Over WebTransport, and over the WebSocket when WebTransport cannot connect (here: UDP blocked)."""
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield
    cfg = Config(data_dir=tmp_path / "tls", tls=True)
    ensure_certificate(cfg)
    port = free_port()
    web = create_portal(env.app)
    srv = _Server(uvicorn.Config(web, host="127.0.0.1", port=port, log_level="warning",
                                 ssl_context_factory=lambda _c, _d: ssl_context(cfg), ws_per_message_deflate=False))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.01)
    gate = None
    if transport == "webtransport":
        from cloudora.webtransport import WebTransportServer
        gate = WebTransportServer(env.app, web.state.portal)
        assert await gate.start("127.0.0.1", port)
        web.state.portal.webtransport = gate
    else:                       # offered, but nothing answers on UDP: the viewer must fall back to the WebSocket
        class Blocked:
            def offer(self):
                return f"{free_port()},{base64.b64encode(bytes(32)).decode()}"
        web.state.portal.webtransport = Blocked()
    base = f"https://127.0.0.1:{port}"
    env.screen.change(0, 0, 320, 200, (40, 30, 20))
    env.screen.change(160, 100, 64, 48, (0, 0, 255))        # a red block (BGR) in the lower right quarter
    try:
        async with firefox(tmp_path) as (b, ctx):
            await b.cmd("browsingContext.navigate", context=ctx, url=base + "/login", wait="complete")
            await b.js(ctx, f"document.getElementById('key').value={json.dumps(env.key)};"
                            "document.querySelector('form').requestSubmit();")
            await b.until(ctx, "location.pathname === '/' && !!document.getElementById('screen')")
            await b.until(ctx, "document.getElementById('screen').width === 320")
            await b.until(ctx, "document.getElementById('msg').hidden")
            assert env.app.desktop.viewer.ws.__class__.__name__ == ("Channel" if transport == "webtransport"
                                                                     else "WebSocket")
            # nothing but the screen: no menus, no buttons, no toolbar
            assert await b.js(ctx, "document.querySelectorAll('select, header, nav, button:not(#msg-retry)').length") == 0
            pixel = await b.js(ctx, "Array.from(document.getElementById('screen').getContext('2d')"
                                    ".getImageData(180, 120, 1, 1).data).join(',')")
            r, g, bl, _ = map(int, pixel.split(","))
            assert r > 200 and g < 60 and bl < 60, pixel                     # the red block arrived
            env.screen.change(10, 10, 32, 32, (0, 255, 0))                   # green: a change, sent as a patch
            await b.until(ctx, "(d => d[1] > 200 && d[0] < 60)(document.getElementById('screen')"
                               ".getContext('2d').getImageData(20, 20, 1, 1).data)")
            supported = await b.js(ctx, "(async () => { const out = []; if (typeof VideoDecoder !== 'function') "
                                        "return ''; for (const [n, c] of [['AV1', 'av01.0.12M.08'], ['VP9', "
                                        "'vp09.00.50.08'], ['H.264', 'avc1.640032']]) { if ((await VideoDecoder"
                                        ".isConfigSupported({codec: c, optimizeForLatency: true})).supported) "
                                        "out.push(n); } return out.join(','); })()")
            from cloudora.desktop import video
            names = {"av1": "AV1", "vp9": "VP9", "h264": "H.264", "hevc": "HEVC"}
            options = video.plan([])                                      # the stand-in screen: no zero-copy GPU
            kind, engine = video.choose(options, [k for k, n in names.items() if n in supported.split(",")])
            for _ in range(50):                                           # the best codec both sides have
                if env.app.desktop.viewer and env.app.desktop.viewer.settings.codec == kind:
                    break
                await asyncio.sleep(0.1)
            assert env.app.desktop.viewer.settings.codec == kind, (kind, supported)
            print(f"\nviewer: {names.get(kind, 'JPEG')} ({engine}); browser decodes: {supported or 'no video'}")
            if kind != "jpeg":
                # A jittery link (Wi-Fi): pictures seem to arrive up to 60 ms late at random. The viewer measures it,
                # holds pictures in a small buffer and shows them at an even pace - and they still all get drawn.
                import random
                from cloudora.desktop import service as service_mod
                real_pack = service_mod.pack_video
                monkeypatch.setattr(service_mod, "pack_video", lambda *a, at_ms=0: real_pack(
                    *a, at_ms=at_ms - random.randint(0, 60)))
                for i in range(40):
                    env.screen.change(100, 10, 32, 32, (0, 0, 255) if i % 2 else (255, 0, 0))
                    await asyncio.sleep(0.03)
                env.screen.change(100, 10, 32, 32, (0, 255, 0))
                await b.until(ctx, "(d => d[1] > 200 && d[0] < 60)(document.getElementById('screen')"
                                   ".getContext('2d').getImageData(110, 20, 1, 1).data)")
                await asyncio.sleep(1.2)
                buffered = int(await b.js(ctx, "document.getElementById('stage').dataset.smoothing || '-1'"))
                assert 10 <= buffered <= 50, buffered                    # follows the jitter, within its limit
            box = await b.js(ctx, "JSON.stringify(document.getElementById('screen').getBoundingClientRect())")
            box = json.loads(box)
            cx, cy = round(box["x"] + box["width"] * 0.75), round(box["y"] + box["height"] * 0.25)
            await b.cmd("input.performActions", context=ctx, actions=[
                {"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": [
                    {"type": "pointerMove", "x": cx, "y": cy}, {"type": "pointerDown", "button": 0},
                    {"type": "pointerUp", "button": 0}, {"type": "pointerDown", "button": 2},
                    {"type": "pointerUp", "button": 2}]},
                {"type": "key", "id": "kb", "actions": [
                    {"type": "pause"}, {"type": "pause"}, {"type": "pause"}, {"type": "pause"}, {"type": "pause"},
                    {"type": "keyDown", "value": ""}, {"type": "keyDown", "value": "A"},
                    {"type": "keyUp", "value": "A"}, {"type": "keyUp", "value": ""}]}])
            await b.cmd("input.releaseActions", context=ctx)
            for _ in range(100):
                if ("key", "ShiftLeft", False) in env.screen.events:
                    break
                await asyncio.sleep(0.05)
            ev = env.screen.events
            assert ("button", 0, True) in ev and ("button", 0, False) in ev and ("button", 2, True) in ev
            moves = [e for e in ev if e[0] == "move"]
            assert moves and abs(moves[-1][2] - 0.75) < 0.02 and abs(moves[-1][3] - 0.25) < 0.02
            keys = [e for e in ev if e[0] == "key"]
            assert keys == [("key", "ShiftLeft", True), ("key", "KeyA", True), ("key", "KeyA", False),
                            ("key", "ShiftLeft", False)]
            shot = await b.cmd("browsingContext.captureScreenshot", context=ctx)
            (tmp_path / "viewer.png").write_bytes(base64.b64decode(shot["data"]))
            out = os.environ.get("CLOUDORA_BROWSER_SCREENSHOT")
            if out:
                shutil.copy(tmp_path / "viewer.png", out)
    finally:
        env.app.sessions.end_all("test over")
        await env.app.desktop.close()
        if gate is not None:
            gate.close()
        srv.should_exit = True
        await task
