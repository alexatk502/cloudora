"""Cloudora: wires the parts together and runs the web server (sign-in page and the screen viewer)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path
from typing import Awaitable, Callable

from cloudora.config import Config
from cloudora.desktop.service import DesktopService
from cloudora.secret import AttemptLimiter, Challenges, SecretKey
from cloudora.sessions import Sessions

log = logging.getLogger("cloudora")


def check_ports(cfg: Config) -> None:
    """Fail early and clearly if another program (usually an earlier Cloudora) already uses the port."""
    import socket
    with socket.socket(socket.AF_INET6 if ":" in cfg.host else socket.AF_INET) as probe:
        if os.name != "nt":   # on Windows SO_REUSEADDR would let two programs share the port
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((cfg.host, cfg.port))
        except OSError:
            raise SystemExit(f"Cloudora: port {cfg.port} is already in use - Cloudora is probably still running from an "
                             "earlier start. Stop it first (Ctrl+C in its window), or choose another with --port.") from None


class Cloudora:
    def __init__(self, config: Config, key: SecretKey, key_file: Path | None = None,
                 desktop: DesktopService | None = None):
        self.config = config
        self.key = key
        self.key_file = key_file
        self.on_new_key: Callable[[SecretKey], None] | None = None   # shows a new key (terminal only)
        self.attempts = AttemptLimiter()
        self.challenges = Challenges()
        self.sessions = Sessions(config.session_hours, config.idle_minutes, config.max_sessions)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(config.data_dir, 0o700)
        self.desktop = desktop or DesktopService(config.data_dir, view_only=config.view_only)
        self.sessions.on_end.append(self.desktop.session_ended)
        self.stopping = asyncio.Event()
        self.webtransport = None          # the WebTransport (QUIC) server, when it runs

    def stop(self) -> None:
        self.stopping.set()

    def new_key(self) -> SecretKey:
        """Replace the key and sign everyone out at once, without stopping (press N + Enter, or send SIGHUP).
        The old key stops working immediately, also for sign-in pages already open."""
        key = SecretKey()
        if self.key_file:
            key.save(self.key_file)
        self.key = key
        self.challenges.clear()
        ended = self.sessions.end_all("a new key was made")
        log.warning("new secret key made; %d sign-in(s) ended", ended)
        if self.on_new_key:
            try:
                self.on_new_key(key)
            except Exception:  # noqa: BLE001 - showing the key must never break Cloudora
                pass
        return key

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(15)
            self.sessions.sweep()

    async def serve(self, on_ready: Callable[[], Awaitable[None]] | None = None) -> None:
        """Run until stop() or Ctrl+C / SIGTERM. Every sign-in and the screen connection end with it."""
        import uvicorn

        from cloudora.portal.app import create_portal

        cfg = self.config
        problem = cfg.problem()
        if problem:
            raise SystemExit(f"Cloudora: refusing to start: {problem}")
        check_ports(cfg)
        ssl_factory = None
        if cfg.tls:
            from cloudora.tls import ensure_certificate, ssl_context
            ensure_certificate(cfg)
            ssl_factory = lambda _cfg, _default: ssl_context(cfg)  # noqa: E731

        class _Server(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):   # Cloudora handles the signals itself (clean shutdown below)
                yield

        web = create_portal(self)
        server = _Server(uvicorn.Config(
            web, host=cfg.host, port=cfg.port, log_level="warning",
            access_log=False,  # no request logs: nothing about visitors is recorded
            server_header=False, date_header=False, proxy_headers=False, ssl_context_factory=ssl_factory,
            limit_concurrency=cfg.max_connections, timeout_keep_alive=15, h11_max_incomplete_event_size=32 * 1024,
            ws_max_size=64 * 1024,             # viewers only send small input messages
            ws_per_message_deflate=False,      # pictures are JPEG already: compressing again only adds delay
            ws_ping_interval=20, ws_ping_timeout=20, timeout_graceful_shutdown=5))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self.stop)
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError, AttributeError):
            loop.add_signal_handler(signal.SIGHUP, self.new_key)
        task = asyncio.ensure_future(server.serve())
        sweeper = asyncio.ensure_future(self._sweep())
        ready_task = None
        try:
            while not server.started:
                if task.done():
                    raise SystemExit(f"Cloudora: could not listen on port {cfg.port} - is another Cloudora still running?")
                await asyncio.sleep(0.05)
            if cfg.tls:                     # direct HTTPS: also offer WebTransport (QUIC, same port number on UDP)
                from cloudora.webtransport import WebTransportServer
                wt = WebTransportServer(self, web.state.portal)
                if await wt.start(cfg.host, cfg.port):
                    self.webtransport = web.state.portal.webtransport = wt
            if on_ready is not None:
                ready_task = asyncio.ensure_future(on_ready())
            stopper = asyncio.ensure_future(self.stopping.wait())
            await asyncio.wait([stopper, task], return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
        finally:
            # Shutdown must always finish: every step is bounded.
            async def bounded(aw, seconds: float, what: str) -> None:
                try:
                    await asyncio.wait_for(aw, seconds)
                except asyncio.TimeoutError:
                    log.warning("shutdown: %s did not finish within %.0fs - continuing", what, seconds)
                except (Exception, SystemExit):  # noqa: BLE001
                    log.warning("shutdown: %s failed", what, exc_info=True)
            for t in (sweeper, ready_task):
                if t is not None:
                    t.cancel()
            self.sessions.end_all("Cloudora is stopping")
            await bounded(self.desktop.close(), 10, "the screen connection")
            if self.webtransport is not None:
                self.webtransport.close()
            server.should_exit = True
            await bounded(task, 10, "web server")
            for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
