"""Web portal: secret-key sign-in (one-time proof), CSRF, cookies, CSP and the viewer page."""

from __future__ import annotations

import re

import httpx
import pytest

from cloudora.portal.app import create_portal
from tests.signin import form

ORIGIN = "https://portal.test"


@pytest.fixture
async def portal(env):
    app = create_portal(env.app)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
                               base_url=ORIGIN, follow_redirects=False)
    client.env = env
    yield client
    await client.aclose()


async def web_login(client, key=None, expect=303):
    page = (await client.get("/login")).text
    r = await client.post("/login", data=form(page, client.env.key if key is None else key, ORIGIN),
                          headers={"Origin": ORIGIN})
    assert r.status_code == expect, r.text[:500]
    return r


# ------------------------------------------------------------------ authentication
async def test_requires_login(portal):
    r = await portal.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await portal.get("/login")
    assert r.status_code == 200 and "secret key" in r.text.lower()


async def test_login_sets_hardened_cookie_and_shows_the_screen(env, portal):
    r = await web_login(portal)
    cookie = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("__Host-cloudora=")][0]
    for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert flag.lower() in cookie.lower()
    assert "domain=" not in cookie.lower()
    home = await portal.get("/")
    assert home.status_code == 200 and 'id="screen"' in home.text
    csp = home.headers["content-security-policy"]
    assert csp.startswith("default-src 'none'") and "script-src 'self'" in csp and "unsafe" not in csp
    assert "wss://portal.test" in csp and "frame-ancestors 'none'" in csp
    assert home.headers["x-frame-options"] == "DENY"
    assert re.findall(r"<script[^>]*>", home.text) == [re.search(r'<script src="/static/viewer\.js\?v=\w+">',
                                                                 home.text).group(0)]   # only Cloudora's own viewer
    js = await portal.get("/static/viewer.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")


async def test_login_csrf_and_origin_checks(env, portal):
    data = form((await portal.get("/login")).text, env.key, ORIGIN)
    for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
        r = await portal.post("/login", data=data, headers=headers)
        assert r.status_code == 403
    r = await portal.post("/login", data={**data, "csrf": "forged"}, headers={"Origin": ORIGIN})
    assert r.status_code == 403
    assert "__Host-cloudora=" not in r.headers.get("set-cookie", "")
    assert env.app.sessions.all() == []


async def test_only_the_secret_key_signs_in(env, portal):
    from cloudora.secret import SecretKey
    for wrong in ("", "CLDR-WRONG", SecretKey().plain, env.key[:-1]):
        r = await web_login(portal, wrong, expect=401)
        assert "not this computer" in r.text and env.key not in r.text
        env.app.attempts._locks.clear()
        env.app.attempts._fails.clear()
    assert env.app.sessions.all() == []
    await web_login(portal, env.key.lower().replace("-", " "))         # typed by hand: still the key
    assert len(env.app.sessions.all()) == 1


async def test_the_key_itself_is_never_accepted_over_the_wire(env, portal):
    page = (await portal.get("/login")).text
    data = {**form(page, env.key, ORIGIN), "key": env.key}
    r = await portal.post("/login", data=data, headers={"Origin": ORIGIN})
    assert r.status_code == 400 and env.app.sessions.all() == []


async def test_a_proof_works_once_and_only_for_this_address(env, portal):
    page = (await portal.get("/login")).text
    data = form(page, env.key, ORIGIN)
    assert (await portal.post("/login", data=data, headers={"Origin": ORIGIN})).status_code == 303
    portal.cookies.clear()
    page = (await portal.get("/login")).text
    replay = {**form(page, env.key, ORIGIN), "challenge": data["challenge"], "proof": data["proof"]}
    assert (await portal.post("/login", data=replay, headers={"Origin": ORIGIN})).status_code == 403
    other = form((await portal.get("/login")).text, env.key, "https://elsewhere.example")
    assert (await portal.post("/login", data=other, headers={"Origin": ORIGIN})).status_code == 401


async def test_wrong_keys_are_rate_limited(env, portal):
    for _ in range(env.app.attempts.per_source):
        await web_login(portal, "CLDR-guess", expect=401)
    r = await web_login(portal, expect=429)                     # even the right key waits out the lock-out
    assert "too many" in r.text.lower() and env.app.sessions.all() == []


async def test_new_key_signs_everyone_out(env, portal):
    await web_login(portal)
    old = env.key
    env.app.new_key()
    assert env.app.sessions.all() == []
    assert (await portal.get("/")).status_code == 303
    await web_login(portal, old, expect=401)


async def test_no_accounts_anywhere(portal):
    for path in ("/register", "/admin", "/admin/add", "/guest", "/device", "/api/rpc", "/docs", "/openapi.json",
                 "/go", "/p/http/example.com/", "/js/open", "/rb"):
        assert (await portal.get(path)).status_code in (404, 405), path
    page = (await portal.get("/login")).text
    # the key is all it asks for (and it is sent only as a proof)
    assert re.findall(r'<input[^>]* name="([a-z_]+)"', page) == ["csrf", "challenge", "proof", "key"]


async def test_forwarded_for_not_trusted_by_default(env, portal):
    for _ in range(env.app.attempts.per_source):
        page = (await portal.get("/login")).text
        await portal.post("/login", data=form(page, "CLDR-guess", ORIGIN),
                          headers={"Origin": ORIGIN, "X-Forwarded-For": "6.6.6.6", "CF-Connecting-IP": "6.6.6.6"})
    assert "6.6.6.6" not in env.app.attempts._locks and "127.0.0.1" in env.app.attempts._locks


async def test_unknown_page_is_a_cloudora_404(portal):
    r = await portal.get("/no-such-page")
    assert r.status_code == 404 and "Page not found" in r.text and "<script" not in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"] and '"detail"' not in r.text


async def test_no_visitor_data_logged(env, portal, caplog):
    caplog.set_level("DEBUG")
    await web_login(portal)
    portal.cookies.clear()
    await web_login(portal, "CLDR-AAAAA-BBBBB", expect=401)
    dump = caplog.text
    assert env.key not in dump and "127.0.0.1" not in dump and "50000" not in dump
