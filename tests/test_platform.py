"""Static and host properties: where Cloudora keeps files, that it keeps nothing about viewers, and which programs it
may ever start."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

import pytest

from cloudora import config

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "cloudora"


def test_data_dir_location(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDORA_DATA_DIR", str(tmp_path / "x"))
    assert config.default_data_dir() == tmp_path / "x"
    monkeypatch.delenv("CLOUDORA_DATA_DIR")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert config.default_data_dir() == tmp_path / "data" / "cloudora"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert config.default_data_dir() == tmp_path / "local" / "cloudora"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert config.default_data_dir() == Path.home() / "Library" / "Application Support" / "cloudora"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_data_directory_is_private_and_holds_nothing_about_viewers(env):
    cfg = env.app.config
    assert stat.S_IMODE(os.stat(cfg.data_dir).st_mode) == 0o700
    env.sign_in()
    assert list(cfg.data_dir.rglob("*")) == []      # no database, no log, no history, no recordings


def test_no_database_accounts_or_recording_in_the_code():
    for py in SRC.rglob("*.py"):
        text = py.read_text()
        for token in ("sqlite", "argon2", "UserStore", "password_hash", "def register", "users.create",
                      "VideoWriter", ".mp4", ".webm", "imwrite"):
            assert token not in text, f"{py.relative_to(SRC)} contains {token!r}"
    js = (SRC / "portal" / "viewer.js").read_text()
    for token in ("MediaRecorder", "captureStream", "download", "eval(", "innerHTML", "new Function"):
        assert token not in js, f"viewer.js contains {token!r}"


def test_cloudora_starts_only_known_programs():
    """No shell, eval or pickle anywhere. Exactly two files start programs, each with a fixed argument list:
    anywhere.py (cloudflared, only with --anywhere) and desktop/wayland.py (gst-launch-1.0 for the picture, and
    pw-link when the session manager does not connect it)."""
    forbidden = ("os.system", "os.popen", "os.exec", "os.spawn", "pty.", "eval(", "exec(", "pickle", "shell=True",
                 "create_subprocess")
    starters = {"anywhere.py": ['[str(cloudflared), "tunnel", "--no-autoupdate", "--url"'],
                "wayland.py": ['[self.gst, "-v", "pipewiresrc"', "[pw_link, str(self.node), name]"]}
    for py in SRC.rglob("*.py"):
        text = py.read_text()
        for token in forbidden:
            assert token not in text, f"{py.name} contains {token!r}"
        if py.name in starters:
            calls = re.findall(r"subprocess\.(?:Popen|run)\(", text)
            assert len(calls) == len(starters[py.name]), py.name
            for args in starters[py.name]:
                assert args in text, (py.name, args)
        else:
            assert "subprocess" not in text, py.name


def test_nothing_is_tied_to_one_machine():
    """No absolute home paths, user names or host names in the code or docs: a fresh download runs anywhere."""
    for path in [*SRC.rglob("*.py"), SRC / "portal" / "viewer.js", *ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md"),
                 *(ROOT / "scripts").glob("*")]:
        text = path.read_text(errors="ignore")
        assert not re.search(r"/home/[a-z]|C:\\\\Users\\\\[A-Za-z]|/Users/[a-z]", text), path


def test_secret_files_gitignored():
    gi = (ROOT / ".gitignore").read_text()
    for pattern in ("*.pem", "*.key", "*.har", ".test-tmp/", "wayland-restore-token"):
        assert pattern in gi
