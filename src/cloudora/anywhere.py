"""--anywhere: a public https:// address with no domain, no router changes and nothing installed on viewing devices.

Uses a Cloudflare *quick tunnel* (cloudflared, free, no account). The address has a certificate every browser
trusts, and the traffic is ordinary HTTPS on port 443, so it works from almost any network.

What Cloudflare can see: the tunnel ends TLS at Cloudflare, so Cloudflare could see the screen pictures and the
keyboard/mouse events passing through it (the secret key itself never travels: sign-in sends a one-time proof, see
cloudora/secret.py). Without --anywhere, nothing outside this computer is involved.

cloudflared is found on PATH, or downloaded from its official GitHub release and run only if its SHA-256 matches the
digest GitHub publishes for that release.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import subprocess  # nosec B404 - cloudflared only: fixed argument lists, no shell
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

URL_RE = re.compile(os.environ.get("CLOUDORA_TUNNEL_URL_RE", r"https://[a-z0-9-]+\.trycloudflare\.com"))
RELEASES = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
EXE = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"


def say(msg: str) -> None:
    print(f"[cloudora] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------------- cloudflared
def _asset_name() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm",
            "x86": "386", "i386": "386", "i686": "386"}.get(machine)
    if arch is None:
        raise SystemExit(f"cloudflared download is not automated for {sys.platform}/{machine} - install cloudflared "
                         "yourself (https://github.com/cloudflare/cloudflared) and put it on PATH.")
    if sys.platform == "win32":
        return f"cloudflared-windows-{arch}.exe"
    if sys.platform == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return f"cloudflared-linux-{arch}"


def find_or_download(bin_dir: Path) -> Path:
    override = os.environ.get("CLOUDORA_CLOUDFLARED")
    if override:
        return Path(override)
    on_path = shutil.which("cloudflared")
    if on_path:
        return Path(on_path)
    target = bin_dir / EXE
    if target.exists():
        return target
    name = _asset_name()
    say("downloading cloudflared (Cloudflare's tunnel client, ~40 MB) from its official GitHub release...")
    with urllib.request.urlopen(urllib.request.Request(RELEASES, headers={"Accept": "application/vnd.github+json"}),
                                timeout=30) as r:  # nosec B310 - fixed https URL
        release = json.load(r)
    asset = next((a for a in release.get("assets", []) if a.get("name") == name), None)
    digest = (asset or {}).get("digest") or ""
    if asset is None or not digest.startswith("sha256:"):
        raise SystemExit("could not find a cloudflared release with a published SHA-256 digest - not downloading an "
                         "unverifiable program. Install cloudflared yourself (https://github.com/cloudflare/cloudflared).")
    bin_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(bin_dir, 0o700)
    with urllib.request.urlopen(asset["browser_download_url"], timeout=300) as r:  # nosec B310 - https GitHub
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != digest.split(":", 1)[1]:
        raise SystemExit(f"cloudflared checksum MISMATCH (expected {digest}, got sha256:{got}) - refusing to run it.")
    if name.endswith(".tgz"):     # macOS releases are archives holding the single program
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            member = next((m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "cloudflared"), None)
            if member is None:
                raise SystemExit("the cloudflared archive does not contain the program - install it yourself.")
            data = tar.extractfile(member).read()
    with tempfile.NamedTemporaryFile(dir=bin_dir, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    os.chmod(tmp_path, 0o700)
    os.replace(tmp_path, target)
    say(f"cloudflared {release.get('tag_name')} verified (sha256 {got[:16]}...) -> {target}")
    return target


def start_tunnel(cloudflared: Path, port: int, log_path: Path) -> tuple[subprocess.Popen, str]:
    log = open(log_path, "w")
    extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else \
        {"start_new_session": True}
    proc = subprocess.Popen(  # nosec B603 - fixed arguments
        [str(cloudflared), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **extra)
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"cloudflared exited early - see {log_path}:\n" + log_path.read_text()[-1500:])
        m = URL_RE.search(log_path.read_text(errors="ignore"))
        if m:
            return proc, m.group(0)
        time.sleep(0.3)
    proc.terminate()
    raise SystemExit(f"cloudflared did not report a public address within 90 s - see {log_path}. This computer's "
                     "network may block Cloudflare; connect on the same network instead (without --anywhere).")


# ---------------------------------------------------------------------------------------------------- self-check
def self_check(portal: str) -> list[tuple[bool, str]]:
    """End-to-end through the public address, exactly as a viewer's browser would go."""
    import ssl
    results = []

    def get(url: str) -> tuple[int, str]:
        req = urllib.request.Request(url, headers={"User-Agent": "cloudora-selfcheck"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:  # nosec B310
                return r.status, r.read(4000).decode(errors="ignore")
        except urllib.error.HTTPError as exc:
            return exc.code, ""
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc)
    for _attempt in range(20):  # a fresh quick tunnel can take a few seconds to propagate
        code, _ = get(portal + "/healthz")
        if code == 200:
            break
        time.sleep(1.5)
    results.append((code == 200, f"reachable with a trusted certificate: {portal}"))
    code, body = get(portal + "/login")
    results.append((code == 200 and "challenge" in body, "sign-in page loads through the tunnel"))
    try:
        import asyncio

        from websockets.asyncio.client import connect

        async def ws_check() -> bool:
            ws_url = portal.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
            try:
                async with connect(ws_url, additional_headers={"Origin": portal}, open_timeout=20) as ws:
                    await asyncio.wait_for(ws.recv(), 10)
                return False  # must NOT be accepted without signing in
            except Exception as exc:  # noqa: BLE001
                # Correct outcome: the handshake reached Cloudora and Cloudora refused it for lack of a sign-in (HTTP 403
                # during the handshake). A tunnel that cannot carry WebSockets fails differently.
                text = str(exc)
                return "403" in text or "4401" in text
        ok = asyncio.run(ws_check())
    except Exception as exc:  # noqa: BLE001
        ok = False
        say(f"websocket check error: {exc}")
    results.append((ok, "screen WebSocket passes the tunnel and refuses anyone not signed in"))
    return results


# ---------------------------------------------------------------------------------------------------- tunnel
class Tunnel:
    """Starts the quick tunnel; stop() ends it (also when Cloudora fails to start)."""

    def __init__(self, data_dir: Path, port: int):
        self.data_dir, self.port = data_dir, port
        self.proc: subprocess.Popen | None = None
        self.url = ""

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        cloudflared = find_or_download(self.data_dir / "bin")
        say("starting a Cloudflare quick tunnel...")
        self.proc, self.url = start_tunnel(cloudflared, self.port, self.data_dir / "tunnel.log")

    def broken(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def stop(self) -> None:
        p = self.proc
        if p is None:
            return
        if p.poll() is None:
            try:
                if sys.platform == "win32":
                    p.terminate()
                else:
                    os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()
