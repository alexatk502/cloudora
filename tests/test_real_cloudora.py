"""The real `cloudora` command in its own process, reached the way another device reaches it.

* Default mode: HTTPS on all interfaces, reached through this machine's LAN address with the certificate pinned.
* --anywhere: through a stand-in for Cloudflare's tunnel (tests/fake_cloudflared.py + fake_edge.py).

The process runs with CLOUDORA_DESKTOP=none, so these tests never touch this computer's real screen; the viewer then
gets Cloudora's explanation that no desktop is available, which proves the whole path (TLS, sign-in, WebSocket) works.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import ssl
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from cryptography import x509

from cloudora.config import Config
from cloudora.tls import ensure_certificate, local_addresses, ssl_context
from tests.real_cloudora import ROOT, Browser, RealCloudora, lan_address

NO_SCREEN = {"CLOUDORA_DESKTOP": "none"}


def test_certificate_contents(tmp_path):
    cfg = Config(data_dir=tmp_path, tls=True)
    fp = ensure_certificate(cfg)
    cert = x509.load_pem_x509_certificate(cfg.cert_path.read_bytes())
    ips = {str(i) for i in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
           .get_values_for_type(x509.IPAddress)}
    assert "127.0.0.1" in ips and set(local_addresses()) <= ips
    if sys.platform != "win32":
        assert stat.S_IMODE(cfg.key_path.stat().st_mode) == 0o600
    assert ensure_certificate(cfg) == fp  # stable across restarts (browsers remember it)
    assert ensure_certificate(cfg, regenerate=True) != fp
    assert ssl_context(cfg).minimum_version == ssl.TLSVersion.TLSv1_3


@pytest.fixture(scope="module")
def lan_cloudora(tmp_path_factory):
    lan = lan_address()
    if not lan:
        pytest.skip("this machine has no LAN address")
    sp = RealCloudora(tmp_path_factory.mktemp("lan"), host=lan, env=NO_SCREEN).start()
    yield sp
    out = sp.stop()
    assert sp.key not in out  # the key never goes to a non-terminal output (service logs)
    assert "SCREEN: NOT READY" in out and "graphical desktop" in out


def pinned(cert: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(cert))  # trust exactly this certificate, verify hostname = IP
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


def login(c: httpx.Client, sp: RealCloudora, key: str):
    from tests.signin import form
    return c.post("/login", data=form(c.get("/login").text, key, sp.portal), headers={"Origin": sp.portal})


async def open_screen(url: str, origin: str, cookie: str, token: str, ctx: ssl.SSLContext | None) -> dict:
    from websockets.asyncio.client import connect
    async with connect(url, additional_headers={"Origin": origin, "Cookie": cookie}, ssl=ctx,
                       open_timeout=20) as ws:
        await ws.send(json.dumps({"k": token}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if m["t"] in ("err", "hello"):
                return m


def test_https_on_lan_address_sign_in_and_screen(lan_cloudora):
    ctx = pinned(lan_cloudora.data / "tls" / "portal-cert.pem")
    with httpx.Client(base_url=lan_cloudora.portal, verify=ctx, timeout=20, follow_redirects=False) as c:
        r = c.get("/login")                       # hostname verification against the IP SAN succeeds
        assert r.status_code == 200 and 'name="challenge"' in r.text
        r = login(c, lan_cloudora, "CLDR-WRONG")
        assert r.status_code == 401 and "__Host-cloudora=" not in r.headers.get("set-cookie", "")
        r = login(c, lan_cloudora, lan_cloudora.key)
        assert r.status_code == 303
        session = re.search(r"__Host-cloudora=([^;]+)", r.headers["set-cookie"]).group(1)
        assert "secure" in r.headers["set-cookie"].lower()
        home = c.get("/", headers={"Cookie": f"__Host-cloudora={session}"})
        assert home.status_code == 200 and 'id="screen"' in home.text
        token = re.search(r'data-token="([^"]+)"', home.text).group(1)
    wss = f"wss://{lan_cloudora.host}:{lan_cloudora.port}/ws"
    m = asyncio.run(open_screen(wss, lan_cloudora.portal, f"__Host-cloudora={session}", token, ctx))
    assert m["t"] == "err" and "graphical desktop" in m["msg"]


def test_tls12_refused(lan_cloudora):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((lan_cloudora.host, lan_cloudora.port), 5) as raw:
        with pytest.raises(ssl.SSLError):
            ctx.wrap_socket(raw, server_hostname=lan_cloudora.host)


def test_plain_http_on_tls_port_gets_nothing(lan_cloudora):
    with socket.create_connection((lan_cloudora.host, lan_cloudora.port), 5) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        s.settimeout(5)
        try:
            data = s.recv(4096)
        except (ConnectionResetError, TimeoutError):
            data = b""
    assert b"HTTP/1.1 200" not in data and b"Cloudora" not in data


def test_refuses_to_start_when_the_port_is_taken(tmp_path):
    sp = RealCloudora(tmp_path)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", sp.port))
        busy.listen()
        r = subprocess.run([sys.executable, "-m", "cloudora", "--local", "--data-dir", str(sp.data), "--port",
                            str(sp.port)], env=sp.env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "still running" in r.stdout + r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the fake tunnel is a POSIX script")
def test_anywhere_through_a_tunnel(tmp_path):
    fake = ROOT / "tests" / "fake_cloudflared.py"
    fake.chmod(fake.stat().st_mode | 0o111)
    sp = RealCloudora(tmp_path, "--anywhere", env={**NO_SCREEN, "CLOUDORA_CLOUDFLARED": str(fake),
                                                 "CLOUDORA_TUNNEL_URL_RE": r"http://127\.0\.0\.1:\d+"}).start()
    try:
        log = (sp.data / "tunnel.log")
        for _ in range(100):
            if log.exists() and "Visit it at" in log.read_text():
                break
            import time
            time.sleep(0.1)
        public = re.search(r"http://127\.0\.0\.1:\d+", log.read_text()).group(0)
        b = Browser(public)
        r = b.login(sp.key)
        assert r.status_code == 303, r.text[:300]
        home = b.get("/")
        token = re.search(r'data-token="([^"]+)"', home.text).group(1)
        cookie = "; ".join(f"{k}={v}" for k, v in b.cookies.items())
        m = asyncio.run(open_screen(public.replace("http", "ws") + "/ws", public, cookie, token, None))
        assert m["t"] == "err"                          # the WebSocket crossed the tunnel and reached the screen
        sp.wait_for_output("SECRET KEY")                # the start-up banner, after the tunnel self-check
    finally:
        out = sp.stop()
    assert "PASS" in out and "FAIL" not in out and sp.key not in out
