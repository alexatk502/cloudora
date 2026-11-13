"""cloudora - see and control this computer's screen from any other device's web browser, with one secret key.

    cloudora                  from your network (HTTPS):   https://<this computer's IP>:8743
    cloudora --anywhere       from any network:            https://<random>.trycloudflare.com  (Cloudflare tunnel)
    cloudora --local          only from this computer:     http://127.0.0.1:8743               (for trying it out)

Every start prints a new secret key (unless --key-file keeps one). Anyone with the key can see and control this
computer until you stop Cloudora with Ctrl+C, or until you make a new key: type N and press Enter in this window
(or send SIGHUP), which signs everyone out at once.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
from pathlib import Path

from cloudora import __version__
from cloudora.config import Config, default_data_dir
from cloudora.secret import SecretKey

BAR = "=" * 72


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="cloudora", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--anywhere", action="store_true", help="a public address through a Cloudflare quick tunnel")
    where.add_argument("--local", action="store_true", help="only reachable from this computer (plain HTTP)")
    where.add_argument("--host", default="", help="listen only on this address with HTTPS (e.g. a VPN address)")
    ap.add_argument("--port", type=int, default=8743, help="port (default 8743)")
    ap.add_argument("--key-file", type=Path, help="keep the same secret key across restarts (created, readable only "
                                                  "by you)")
    ap.add_argument("--view-only", action="store_true", help="show the screen but ignore the viewer's mouse and "
                                                             "keyboard")
    ap.add_argument("--public-url", default="", help="the address viewers use when you run your own tunnel or "
                                                     "reverse proxy in front of Cloudora")
    ap.add_argument("--data-dir", type=Path, default=None, help=f"default: {default_data_dir()}")
    ap.add_argument("--version", action="version", version=f"cloudora {__version__}")
    return ap.parse_args(argv)


def interactive() -> bool:
    try:
        return sys.stdout.isatty() and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def show_key(key: SecretKey, key_file: Path | None) -> list[str]:
    if interactive():
        lines = ["  SECRET KEY (anyone who has it can see and control this computer):", f"      {key.plain}"]
    else:   # never write the key into a log file
        lines = ["  SECRET KEY: not shown, because this output is not a terminal."]
        if key_file:
            lines.append(f"  It is in {key_file}")
    if key_file:
        lines.append(f"  The key is kept in {key_file}.")
    else:
        lines.append("  A new key is made every time Cloudora starts.")
    if interactive():
        lines.append("  Type N and press Enter for a new key (signs everyone out). Ctrl+C stops Cloudora.")
    return lines


def configure_logging() -> None:
    """Cloudora's own messages only; uvicorn's access log is off, so nothing about visitors is ever written."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore", "uvicorn", "uvicorn.access", "uvicorn.error", "websockets", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    a = parse(argv)
    configure_logging()
    cfg = Config(port=a.port, public_url=a.public_url.rstrip("/"), view_only=a.view_only)
    if a.data_dir:
        cfg.data_dir = a.data_dir
    if a.anywhere or a.local:
        cfg.host, cfg.tls = "127.0.0.1", False         # the tunnel (or this computer) reaches Cloudora on loopback
    elif a.host:
        cfg.host, cfg.tls = a.host, True
    else:
        cfg.host, cfg.tls = "0.0.0.0", True  # nosec B104 - the default: reachable from your network, HTTPS only
    try:
        key = SecretKey.from_file(a.key_file) if a.key_file else SecretKey()
    except ValueError:
        raise SystemExit(f"{a.key_file} does not hold a Cloudora key - delete it to make a new one") from None

    from cloudora.server import Cloudora, check_ports
    problem = cfg.problem()
    if problem:
        raise SystemExit(f"Cloudora: refusing to start: {problem}")
    check_ports(cfg)
    tunnel = None
    if a.anywhere:
        from cloudora.anywhere import Tunnel
        tunnel = Tunnel(cfg.data_dir, cfg.port)
        try:
            tunnel.start()
        except BaseException:
            tunnel.stop()
            raise
        cfg.public_url = tunnel.url
        cfg.trust_cf_connecting_ip = True
    cloudora = Cloudora(cfg, key, key_file=a.key_file)

    def announce(new: SecretKey) -> None:
        print("\n".join([BAR, "  NEW KEY - everyone was signed out; the old key no longer works.",
                         *show_key(new, a.key_file), BAR]), flush=True)
    cloudora.on_new_key = announce

    def watch_terminal(loop: asyncio.AbstractEventLoop) -> None:
        """N + Enter in Cloudora's own window makes a new key (a thread: works the same on Windows, macOS, Linux)."""
        if not interactive():
            return

        def read() -> None:
            for line in sys.stdin:
                if line.strip().lower() in ("n", "new"):
                    loop.call_soon_threadsafe(cloudora.new_key)
        threading.Thread(target=read, name="cloudora-terminal", daemon=True).start()

    async def ready() -> None:
        watch_terminal(asyncio.get_running_loop())
        lines = [BAR]
        if tunnel:
            from cloudora.anywhere import self_check
            results = await asyncio.to_thread(self_check, cfg.public_url)
            lines += [f"  {'PASS' if ok else 'FAIL'}  {text}" for ok, text in results]
            if not all(ok for ok, _ in results):
                lines.append("  Some checks FAILED - see docs/INSTALLATION.md (Troubleshooting).")
            lines += ["", "  OPEN THIS ADDRESS ON ANY DEVICE, ANY NETWORK (nothing to install):", f"      {cfg.public_url}",
                      "  The address changes every time Cloudora starts. Cloudflare carries the connection."]
        else:
            from cloudora.tls import fingerprint, portal_urls, pretty_fingerprint
            lines.append("  OPEN THIS ADDRESS ON THE OTHER DEVICE:" if cfg.tls else "  OPEN:")
            lines += [f"      {u}" for u in portal_urls(cfg)]
            if cfg.tls:
                lines += ["  The browser warns once about Cloudora's own certificate. Its SHA-256 fingerprint is:",
                          f"      {pretty_fingerprint(fingerprint(cfg.cert_path))}",
                          "  From outside your network: forward this port on your router, use your own VPN, or "
                          "start Cloudora with --anywhere."]
        if cfg.view_only:
            lines += ["", "  VIEW ONLY: the viewer's mouse and keyboard are ignored."]
        lines += [""] + show_key(cloudora.key, a.key_file) + [BAR]
        print("\n".join(lines), flush=True)
        # Open the screen once now, so any permission prompt appears while someone is at the computer.
        print("[cloudora] SCREEN: " + await cloudora.desktop.preflight(), flush=True)
        warned = False
        while tunnel and not warned:
            await asyncio.sleep(2)
            if tunnel.broken():
                warned = True
                logging.getLogger("cloudora").warning("the Cloudflare tunnel stopped - restart Cloudora")

    try:
        asyncio.run(cloudora.serve(ready))
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel:
            tunnel.stop()
            print("[cloudora] stopped Cloudora and the tunnel", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
