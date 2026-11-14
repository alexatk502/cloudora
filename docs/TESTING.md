# Testing

## Automated tests

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

They need no internet, no accounts and no display: a stand-in screen (`tests/fake_desktop.py`) records every input
event and makes synthetic pictures, a stand-in tunnel (`tests/fake_edge.py`, `tests/fake_cloudflared.py`) replaces
Cloudflare, and the real `cloudora` command runs in subprocesses with `CLOUDORA_DESKTOP=none` so your real screen is never
touched. The browser test runs headless Firefox and is skipped when Firefox is not installed.

| Area | File | What is checked |
|---|---|---|
| Secret key, sessions, settings, certificate | `test_core.py` | key shape and randomness, constant-time check in any spacing, key file 0600 and refused when readable by others, per-address and global lock-out, sessions in memory (digest only, expiry, idle, limit; never idle while the screen is open), unsafe settings refused, certificate private, stable, TLS 1.3 only, without the computer's name |
| Portal | `test_portal.py` | only the key signs in, and only as a one-time proof for this address (replay and other addresses refused; the key itself refused); wrong keys rate-limited; CSRF and Origin on sign-in; a new key signs everyone out; no account, admin, API or old proxy pages; hardened cookie; the viewer page's CSP allows only Cloudora's own script and no forms; 404 pages; nothing about viewers logged |
| The screen | `test_desktop.py` | changed areas found exactly (randomised) and aligned; whole picture first, then only changes; scaling to the viewer; the key table; the WebSocket needs Origin + cookie + session token (the screen is not even opened otherwise); pictures arrive and match the screen; **flow control: without acknowledgements only a few pictures are in flight, then they flow again; the base delay is never inflated by Cloudora's own queue**; Wayland input messages well formed (fire-and-forget D-Bus calls); **video, for every codec and engine this computer has (AV1/VP9/H.264 software, AV1/HEVC/H.264 on the GPU): the stream decodes (independent decoder), starts with a keyframe, goes quiet on a still screen, sends small deltas for small changes, and makes a keyframe on request**; odd sizes and padded rows; the encoder plan (GPU first, software fallbacks); **zero-copy GPU encoding (VA-API): keyframes on request, live GPU scaling, live quality changes, and the service streaming through it at > 20 fps**; **games: relative motion validated and delivered, constant bitrate changeable live, the resolution ladder; sound sent only while turned on; real timestamps and per-picture budgets for the software encoder**; pointer position; every input type reaches the screen in order; malformed input ignored and positions clamped; **held keys and buttons released when the viewer leaves**; view-only; a new viewer takes over; a new key closes the screen; unavailable screens explained; recovery after a capture failure; the screen closes after the last viewer |
| WebTransport | `test_webtransport.py` | a real QUIC client: pictures on their stream, input in order, sound as datagrams; wrong token sent to sign-in without opening the screen; other origins refused (403); silent connections dropped; take-over and a new key end it; the pinned certificate is what browsers accept (ECDSA P-256, at most 14 days); the congestion window shrinks gently, once per loss episode, never below its floor |
| Real browser | `test_browser.py` | headless Firefox over HTTPS, once over WebTransport and once with UDP blocked (falls back to the WebSocket): signs in through the real sign-in script, the page is just the screen (no menus or buttons), the best codec both sides support is chosen, the canvas shows the right pixels, a change arrives, pictures on a jittery link are paced by a small buffer (10-50 ms) and all drawn, real mouse clicks, right-click and Shift+A reach the screen at the right position |
| Real process | `test_real_cloudora.py` | `cloudora` on this machine's LAN address: IP certificate, sign-in with Secure cookies, the viewer page, the WebSocket over `wss://`, TLS 1.2 refused, no plain HTTP, clear error when the port is taken, the key never in non-terminal output; **`cloudora --anywhere` end to end through a stand-in tunnel** (self-checks pass, WebSocket crosses the tunnel) |
| Host properties | `test_platform.py` | data directory per platform, private, holds nothing about viewers; no database, account or recording code; no shell, `eval`, `exec` or `pickle`; only `cloudflared`, `gst-launch-1.0` and `pw-link` are ever started, with fixed arguments; nothing tied to one machine (no home paths); secret files git-ignored |

## Your real screen (opt in)

```bash
CLOUDORA_LIVE_DESKTOP=1 .venv/bin/python -m pytest -s tests/test_live_desktop.py
CLOUDORA_LIVE_DESKTOP=1 CLOUDORA_LIVE_INPUT=1 .venv/bin/python -m pytest -s tests/test_live_desktop.py   # also moves the pointer
```

Opens this platform's real backend, reads pictures for two seconds and prints the backend, the screen size, the
pictures per second and the time to encode each. On Linux with Wayland the first run shows the desktop's permission
dialog (set `CLOUDORA_LIVE_DATA_DIR` to reuse a directory that already holds a restore token).

Platform coverage: the Linux Wayland backend (on KDE Plasma) and the shared parts are tested on real hardware. The
Windows, macOS and X11 backends follow each platform's documented input and capture APIs but have only been checked
by the automated tests above (which use the stand-in screen); run the opt-in test on each platform before relying
on it.

## Static checks

```bash
.venv/bin/bandit -r src -ll      # no medium or high findings expected
.venv/bin/pip-audit              # known vulnerabilities in dependencies
```
