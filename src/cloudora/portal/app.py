"""The Cloudora portal (FastAPI): sign in with the secret key, then see and control this computer's screen.

Pages: /login (the key, proven with a one-time challenge - the key itself never travels) and / (the screen,
nothing else). One WebSocket, /ws, carries pictures to the viewer and mouse/keyboard events back (cloudora/desktop/service.py).
Before anything is sent on it, the WebSocket checks the Origin (cross-site WebSocket hijacking), the sign-in cookie
and a token bound to that sign-in.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from cloudora import __version__
from cloudora.errors import Codes, CloudoraError
from cloudora.portal import pages
from cloudora.sessions import Session, new_token

if TYPE_CHECKING:
    from cloudora.server import Cloudora

log = logging.getLogger("cloudora.portal")

SESSION_COOKIE = "__Host-cloudora"
CSRF_COOKIE = "__Host-cloudora-csrf"
MAX_FORM = 16 * 1024

PORTAL_CSP = ("default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
              "frame-ancestors 'none'; base-uri 'none'")
COMMON_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), "
                          "bluetooth=(), display-capture=(), interest-cohort=()",
    "Strict-Transport-Security": "max-age=31536000",
    "Server": "cloudora",
}


class Portal:
    def __init__(self, app: "Cloudora"):
        self.app = app
        self.csrf_key = secrets.token_bytes(32)   # new every start: forms from before a restart stop working

    def client_ip(self, request: Request) -> str:
        """The visitor's address, used only to rate-limit wrong keys (kept in memory, never logged)."""
        peer = request.client.host if request.client else ""
        if peer in ("127.0.0.1", "::1") and self.app.config.trust_cf_connecting_ip:
            cf = request.headers.get("cf-connecting-ip", "").strip()
            try:
                return str(ipaddress.ip_address(cf)) if cf else peer
            except ValueError:
                return peer
        return peer

    def _sign(self, value: str) -> str:
        return hmac.new(self.csrf_key, ("portal-csrf:" + value).encode(), hashlib.sha256).hexdigest()

    def own_netlocs(self, host_header: str) -> set[str]:
        """The netlocs this portal is legitimately reached at: the Host header, plus the configured public address
        (a tunnel may present a different Host to Cloudora than the one in the visitor's address bar)."""
        out = {host_header}
        if self.app.config.public_url:
            out.add(urlsplit(self.app.config.public_url).netloc)
        return out

    def _check_csrf(self, request: Request, form: dict[str, str], bound_to: str | None) -> bool:
        origin = request.headers.get("origin") or ""
        if not origin or urlsplit(origin).netloc not in self.own_netlocs(request.headers.get("host", "")):
            return False
        if not bound_to:
            return False
        return hmac.compare_digest(form.get("csrf", ""), self._sign(bound_to))

    def session_from(self, request: Request) -> Session | None:
        return self.app.sessions.resolve(request.cookies.get(SESSION_COOKIE))

    def screen_token(self, session: Session) -> str:
        return self._sign("screen:" + session.id)

    @staticmethod
    async def read_form(request: Request, limit: int = MAX_FORM) -> dict[str, str]:
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise CloudoraError(Codes.BAD_REQUEST, "form too large", 413)
        try:
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CloudoraError(Codes.BAD_REQUEST, "invalid form", 400) from exc
        return {k: v[0] for k, v in parsed.items()}


def html_response(body: str, status: int = 200, cookies: list[tuple[str, str, int | None]] | None = None,
                  script_hash: str | None = None, csp: str | None = None) -> HTMLResponse:
    r = HTMLResponse(body, status_code=status)
    r.headers["Content-Security-Policy"] = csp or (PORTAL_CSP + (f"; script-src {script_hash}" if script_hash else ""))
    r.headers["X-Frame-Options"] = "DENY"
    for name, value, max_age in cookies or []:
        r.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="strict")
    return r


def create_portal(app: "Cloudora") -> FastAPI:
    portal = Portal(app)
    web = FastAPI(title="Cloudora", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    web.state.portal = portal

    @web.middleware("http")
    async def headers(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > MAX_FORM):
            return html_response(pages.error_page("Too large", "That request is too large."), 413)
        response = await call_next(request)
        for k, v in COMMON_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    @web.exception_handler(CloudoraError)
    async def cloudora_error(_request: Request, exc: CloudoraError):
        return html_response(pages.error_page("Request refused", exc.message), exc.http_status)

    @web.exception_handler(404)
    async def not_found(_request: Request, _exc):
        """A mistyped address gets a Cloudora page, not a raw JSON error (still a real 404)."""
        return html_response(pages.error_page("Page not found", "There is no Cloudora page at this address."), 404)

    def csrf_cookie(request: Request) -> tuple[str, list]:
        token = request.cookies.get(CSRF_COOKIE)
        if token and 20 <= len(token) <= 100:
            return token, []
        token = new_token(24)
        return token, [(CSRF_COOKIE, token, None)]

    def login_view(request: Request, status: int = 200, **kw) -> HTMLResponse:
        token, cookies = csrf_cookie(request)
        challenge = app.challenges.issue(bound_to=token)
        return html_response(pages.login_page(portal._sign(token), challenge, **kw), status, cookies,
                             script_hash=pages.LOGIN_JS_HASH)

    # ------------------------------------------------------------------ static
    @web.get("/static/portal.css")
    async def css():
        return Response(pages.CSS, media_type="text/css", headers={"Cache-Control": "max-age=3600"})

    @web.get("/static/viewer.js")
    async def viewer_js():
        return Response(pages.VIEWER_JS, media_type="text/javascript", headers={"Cache-Control": "max-age=3600"})

    @web.get("/healthz")
    async def health():
        return Response("ok", media_type="text/plain")

    @web.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    # ------------------------------------------------------------------ the screen
    @web.get("/")
    async def home(request: Request):
        session = portal.session_from(request)
        if session is None:
            return RedirectResponse("/login", 303)
        host = request.headers.get("host", "")
        csp = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
               f"connect-src 'self' wss://{host} ws://{host}; form-action 'none'; frame-ancestors 'none'; "
               "base-uri 'none'")
        return html_response(pages.viewer_page(portal.screen_token(session)), csp=csp)

    @web.websocket("/ws")
    async def screen_ws(ws: WebSocket):
        origin, host = ws.headers.get("origin", ""), ws.headers.get("host", "")
        if not origin or urlsplit(origin).netloc not in portal.own_netlocs(host):   # cross-site WebSocket hijacking
            await ws.close(code=4403)
            return
        session = app.sessions.resolve(ws.cookies.get(SESSION_COOKIE))
        if session is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            first = json.loads((await asyncio.wait_for(ws.receive_text(), 10))[:512])
            if not isinstance(first, dict) or not hmac.compare_digest(str(first.get("k", "")),
                                                                      portal.screen_token(session)):
                await ws.close(code=4403)
                return
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await ws.close(code=4400)
            return
        if app.sessions.get(session.id) is None:     # signed out while connecting
            await ws.close(code=4401)
            return
        try:
            await app.desktop.serve(ws, session)
        except (WebSocketDisconnect, RuntimeError):
            pass

    # ------------------------------------------------------------------ authentication
    @web.get("/login")
    async def login_get(request: Request):
        if portal.session_from(request):
            return RedirectResponse("/", 303)
        return login_view(request)

    @web.post("/login")
    async def login_post(request: Request):
        form = await portal.read_form(request)
        if not portal._check_csrf(request, form, request.cookies.get(CSRF_COOKIE)):
            return login_view(request, 403, error="Your sign-in form expired. Please try again.")
        source = portal.client_ip(request)
        wait = app.attempts.blocked(source)
        if wait:
            return login_view(request, 429, error=f"Too many wrong keys. Try again in {int(wait) + 1} seconds.")
        if form.get("key"):    # the page's script never sends the key itself; only a proof that it has it
            return login_view(request, 400, error="This sign-in page needs JavaScript, so that the key never leaves "
                                                  "your browser. Please reload the page and try again.")
        fresh = app.challenges.take(form.get("challenge"), bound_to=request.cookies.get(CSRF_COOKIE, ""))
        if not fresh:
            return login_view(request, 403, error="Your sign-in form expired. Please try again.")
        if not app.key.verify_proof(form["challenge"], request.headers.get("origin", ""), form.get("proof")):
            app.attempts.failure(source)
            log.warning("wrong secret key entered")
            return login_view(request, 401, error="That is not this computer's secret key.")
        app.attempts.success(source)
        token, _session = app.sessions.create()
        log.info("someone signed in with the key")
        r = RedirectResponse("/", 303)
        r.set_cookie(SESSION_COOKIE, token, path="/", secure=True, httponly=True, samesite="strict")
        r.delete_cookie(CSRF_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return r

    return web
