from __future__ import annotations

import socket
from dataclasses import dataclass

import pytest

from cloudora.config import Config
from cloudora.desktop.service import DesktopService
from cloudora.secret import SecretKey
from cloudora.server import Cloudora
from cloudora.sessions import Session
from tests.fake_desktop import FakeBackend, Opener


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Env:
    app: Cloudora
    key: str
    screen: FakeBackend
    opener: Opener

    def sign_in(self) -> tuple[str, Session]:
        """A signed-in browser (what POST /login with the right key creates)."""
        return self.app.sessions.create()


def make_env(tmp_path, view_only: bool = False, error: str = "") -> Env:
    key = SecretKey()
    opener = Opener(error=error)
    cfg = Config(data_dir=tmp_path / "cloudora", view_only=view_only)
    app = Cloudora(cfg, key, desktop=DesktopService(cfg.data_dir, opener=opener, view_only=view_only))
    return Env(app, key.plain, opener.backend, opener)


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    try:
        yield e
    finally:
        e.app.sessions.end_all("test over")
