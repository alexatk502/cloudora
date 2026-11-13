"""The small core: secret key, sign-in rate limit, in-memory sessions, settings, HTTPS."""

from __future__ import annotations

import asyncio
import os
import ssl
import time

import pytest

from cloudora import secret
from cloudora.config import Config
from cloudora.secret import AttemptLimiter, SecretKey
from cloudora.sessions import Sessions, hash_token, new_token
from cloudora.tls import ensure_certificate, fingerprint, ssl_context


# ------------------------------------------------------------------ secret key
def test_key_shape_and_randomness():
    keys = {secret.generate() for _ in range(50)}
    assert len(keys) == 50
    for k in keys:
        assert k.startswith("CLDR-") and len(k.split("-")) == 9 and secret.valid_shape(k)
        assert not set(secret.normalise(k)) & set("01OILU")    # nothing that is easy to misread


def test_key_verifies_only_itself_in_any_spacing():
    key = SecretKey()
    assert key.verify(key.plain)
    assert key.verify(key.plain.lower().replace("-", " "))       # typed by hand
    assert key.verify(key.plain[5:])                              # without the CLDR- prefix
    assert not key.verify(SecretKey().plain)
    assert not key.verify("")
    assert not key.verify(None)
    assert not key.verify(key.plain[:-1])
    assert not key.verify(key.plain + "X" * 100)                  # oversized input


def test_key_is_kept_only_as_a_digest(tmp_path):
    key = SecretKey()
    assert key.plain not in repr(vars(key)["_digest"])


def test_key_file_is_private_and_reused(tmp_path):
    path = tmp_path / "keys" / "cloudora.key"
    first = SecretKey.from_file(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert SecretKey.from_file(path).plain == first.plain
    os.chmod(path, 0o644)
    with pytest.raises(SystemExit):     # readable by others: refuse to use it
        SecretKey.from_file(path)
    os.chmod(path, 0o600)
    path.write_text("not a key\n")
    with pytest.raises(ValueError):
        SecretKey.from_file(path)


def test_attempt_limiter_locks_a_source_and_backs_off():
    lim = AttemptLimiter(per_source=3, base_lock=30, global_per_minute=100)
    for _ in range(2):
        lim.failure("1.2.3.4")
    assert lim.blocked("1.2.3.4") == 0
    lim.failure("1.2.3.4")
    assert 29 < lim.blocked("1.2.3.4") <= 30
    assert lim.blocked("5.6.7.8") == 0          # other visitors are not affected
    lim._locks["1.2.3.4"] = (time.monotonic() - 1, 30)
    for _ in range(3):
        lim.failure("1.2.3.4")
    assert 59 < lim.blocked("1.2.3.4") <= 60    # doubled


def test_attempt_limiter_global_cap():
    lim = AttemptLimiter(per_source=1000, global_per_minute=5)
    for i in range(5):
        lim.failure(f"10.0.0.{i}")
    assert lim.blocked("203.0.113.9") > 0       # many addresses together: everyone waits


# ------------------------------------------------------------------ sessions
def test_sessions_live_in_memory_and_end_cleanly():
    s = Sessions()
    ended = []
    s.on_end.append(ended.append)
    token, session = s.create()
    assert session.token_hash == hash_token(token) and token not in repr(session)
    assert s.resolve(token) is session
    assert s.resolve("x" * 43) is None and s.resolve(None) is None and s.resolve("short") is None
    assert s.end(session.id)
    assert ended == [session] and s.resolve(token) is None and s.get(session.id) is None


def test_sessions_expire_and_go_idle():
    s = Sessions(hours=1, idle_minutes=10)
    t1, a = s.create()
    t2, b = s.create()
    a.expires_at = time.time() - 1
    b.last_seen = time.time() - 601
    assert s.resolve(t1) is None and s.resolve(t2) is None
    t3, c = s.create()
    c.last_seen = time.time() - 601
    c.viewers = 1                            # the screen is open: not idle
    assert s.sweep() == 0 and s.get(c.id) is c


def test_session_limit_replaces_the_longest_unused():
    s = Sessions(max_sessions=2)
    _, a = s.create()
    _, b = s.create()
    a.last_seen -= 100
    _, c = s.create()
    assert s.get(a.id) is None and s.get(b.id) is b and s.get(c.id) is c


def test_tokens():
    t = new_token()
    assert len(t) >= 43 and t != new_token() and len(hash_token(t)) == 64


# ------------------------------------------------------------------ settings
@pytest.mark.parametrize("kw,problem", [
    ({"host": "0.0.0.0"}, "HTTPS"),
    ({"host": "192.168.1.20"}, "HTTPS"),
    ({"public_url": "http://cloudora.example"}, "https"),
    ({"public_url": "https://a.example/path"}, "scheme://host"),
])
def test_unsafe_settings_refused(kw, problem):
    assert problem in (Config(**kw).problem() or "")


def test_safe_settings_accepted():
    assert Config().problem() is None
    assert Config(host="0.0.0.0", tls=True).problem() is None
    assert Config(public_url="https://a.example").problem() is None
    assert Config(public_url="http://127.0.0.1:9000").problem() is None


def test_session_with_open_screen_never_goes_idle():
    s = Sessions(hours=1, idle_minutes=1)
    _, a = s.create()
    a.viewers = 1
    a.last_seen -= 3600
    assert s.sweep() == 0 and s.get(a.id) is a
    a.viewers = 0
    assert s.sweep() == 1


# ------------------------------------------------------------------ HTTPS for --lan
async def test_certificate_is_private_stable_and_tls13_only(tmp_path):
    cfg = Config(data_dir=tmp_path / "d", host="0.0.0.0", tls=True)
    fp = ensure_certificate(cfg)
    assert fp == fingerprint(cfg.cert_path) == ensure_certificate(cfg)     # reused, not regenerated
    assert oct(cfg.key_path.stat().st_mode & 0o777) == "0o600"
    assert oct(cfg.tls_dir.stat().st_mode & 0o777) == "0o700"
    import socket

    from cryptography import x509
    sans = x509.load_pem_x509_certificate(cfg.cert_path.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value
    assert sans.get_values_for_type(x509.DNSName) == ["localhost"]     # the computer's name is not published
    assert socket.gethostname() not in sans.get_values_for_type(x509.DNSName)
    srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0, ssl=ssl_context(cfg))
    port = srv.sockets[0].getsockname()[1]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises((ssl.SSLError, ConnectionError)):
        await asyncio.open_connection("127.0.0.1", port, ssl=ctx, server_hostname=None)
    srv.close()


def test_keys_from_before_the_rename_still_work():
    key = SecretKey()
    assert key.plain.startswith("CLDR-")
    old_style = "SPDR-" + key.plain[5:]
    assert key.verify(old_style) and key.verify(key.plain.lower())
    assert SecretKey(old_style).verify(key.plain)          # an old key file keeps working
