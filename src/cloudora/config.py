"""Start-up settings. There is no configuration file and no database: everything comes from command-line flags,
and nothing about visitors is ever written to disk."""

from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def default_data_dir() -> Path:
    """Holds only the self-signed certificate, the verified cloudflared download (--anywhere), the tunnel log and, on
    Linux with Wayland, the desktop's remote-control permission token. Never anything about visitors or the screen."""
    env = os.environ.get("CLOUDORA_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "cloudora"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "cloudora"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "cloudora"


@dataclass
class Config:
    data_dir: Path = field(default_factory=default_data_dir)
    host: str = "127.0.0.1"           # where the portal listens
    port: int = 8743
    tls: bool = False                 # serve HTTPS with a self-signed certificate (required beyond this computer)
    public_url: str = ""              # the portal's address as visitors type it (behind a tunnel or reverse proxy)
    trust_cf_connecting_ip: bool = False   # behind Cloudflare's tunnel: the visitor's address is in CF-Connecting-IP
    view_only: bool = False           # show the screen but ignore mouse and keyboard
    session_hours: float = 12.0       # a sign-in lasts at most this long...
    idle_minutes: int = 60            # ...and ends after this long without use (never while the screen is open)
    max_sessions: int = 8             # signed-in browsers at once
    max_connections: int = 64         # open HTTP connections

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / "tls"

    @property
    def cert_path(self) -> Path:
        return self.tls_dir / "portal-cert.pem"

    @property
    def key_path(self) -> Path:
        return self.tls_dir / "portal-key.pem"

    def problem(self) -> str | None:
        """Fail closed: never serve the portal beyond this machine without HTTPS."""
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host == "localhost"
        if not loopback and not self.tls:
            return "listening beyond this computer needs HTTPS: the secret key's proof and the screen must never " \
                   "travel over plain HTTP"
        if self.public_url:
            parts = urlsplit(self.public_url)
            local = parts.hostname in ("127.0.0.1", "localhost", "::1")
            if parts.scheme != "https" and not (parts.scheme == "http" and local):
                return "public_url must be an https:// address"
            if parts.path not in ("", "/") or parts.query or parts.username or not parts.hostname:
                return "public_url must be just scheme://host[:port]"
        return None
