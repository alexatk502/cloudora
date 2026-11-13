"""Run the real `cloudora` command in a subprocess for end-to-end tests, and a minimal cookie-keeping browser."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lan_address() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
            return s.getsockname()[0]
        except OSError:
            return None


class RealCloudora:
    """`python -m cloudora --key-file ... [args]`; the key is read from the key file (never from the output)."""

    def __init__(self, tmp: Path, *args: str, host: str = "127.0.0.1", env: dict | None = None):
        self.tmp, self.args, self.host = Path(tmp), list(args), host
        self.port = free_port()
        self.data = self.tmp / "data"
        self.key_file = self.tmp / "cloudora.key"
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT), **(env or {})}
        self.proc: subprocess.Popen | None = None
        self.output = ""

    def start(self) -> "RealCloudora":
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "cloudora", "--data-dir", str(self.data), "--port", str(self.port),
             "--key-file", str(self.key_file), *self.args],
            env=self.env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        import threading
        self.lines: list[str] = []
        self._reader = threading.Thread(target=lambda: self.lines.extend(self.proc.stdout), daemon=True)
        self._reader.start()
        for _ in range(300):
            if self.proc.poll() is not None:
                self._reader.join(5)
                raise AssertionError("cloudora exited: " + "".join(self.lines))
            try:
                socket.create_connection((self.host, self.port), 0.3).close()
                break
            except OSError:
                time.sleep(0.1)
        self.key = self.key_file.read_text().strip()
        return self

    def wait_for_output(self, text: str, timeout: float = 30) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            joined = "".join(self.lines)
            if text in joined:
                return joined
            time.sleep(0.1)
        raise AssertionError(f"{text!r} not in output: " + "".join(self.lines))

    def stop(self, sig: int = signal.SIGTERM) -> str:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            self.proc.wait(40)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._reader.join(10)
        self.output = "".join(self.lines)
        return self.output

    @property
    def portal(self) -> str:
        plain = "--local" in self.args or "--anywhere" in self.args
        return f"{'http' if plain else 'https'}://{self.host}:{self.port}"


class Browser:
    """Minimal cookie-handling browser."""

    def __init__(self, base: str, **client_kw):
        self.http = httpx.Client(base_url=base, timeout=60, follow_redirects=False, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"}, **client_kw)
        self.cookies: dict[str, str] = {}
        self.origin = base

    def _store(self, r):
        for k, v in r.headers.multi_items():
            if k == "set-cookie":
                name, value = v.split(";", 1)[0].split("=", 1)
                if "max-age=0" in v.lower() or 'expires=thu, 01 jan 1970' in v.lower():
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = value

    def get(self, path, **kw):
        r = self.http.get(path, headers={"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
                                         "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}, **kw)
        self._store(r)
        return r

    def post(self, path, data):
        r = self.http.post(path, data=data, headers={"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
                                                     "Origin": self.origin})
        self._store(r)
        return r

    def open(self, url):
        """Type an address in the Cloudora box (POST /go) and follow to the encrypted /p/<token> address."""
        r = self.post("/go", {"url": url})
        if r.status_code == 303 and r.headers["location"].startswith("/p/"):
            assert "://" not in r.headers["location"]
            return self.get(r.headers["location"])
        return r

    def login(self, key: str):
        """Sign in the way the sign-in page's script does (a one-time proof; the key itself is never sent)."""
        from tests.signin import form
        return self.post("/login", form(self.get("/login").text, key, self.origin))
