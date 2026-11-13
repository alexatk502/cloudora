# Security and privacy

## 1. What Cloudora protects, and from whom

**Assets:** the host computer (everything its user can do), what is on its screen, and the secret key.

**Considered:**
1. **Strangers** who find the address (on the network, or the public `--anywhere` address). Without the key they get
   the sign-in page and nothing else: no picture, no input, no information about the host.
2. **Other websites** open in the viewer's browser: they must not be able to use the viewer's sign-in to read the
   screen or send input (cross-site requests, cross-site WebSocket hijacking, framing).
3. **Eavesdroppers** on either network (Wi-Fi, school/work filters) and the tunnel provider.
4. **Leftovers:** keys held down when a connection drops, sessions that outlive their use, anything written to disk.

**Not in scope:** someone who has the key. **The key grants full control of the host** - everything its user could
do at the keyboard. There are no per-person permissions. Treat the key like the computer's password.

## 2. What each party can see

| Who | Sees |
|---|---|
| Someone with the key | the host's screen, and can use its mouse and keyboard |
| Someone without the key | the sign-in page |
| The host's user | Cloudora's window logs each sign-in and each viewer connecting/leaving (without addresses). On Linux with Wayland the desktop also shows its own "remote control" notice |
| The networks in between | that a device connects to the host's address (or to `trycloudflare.com`); pictures and input are encrypted (TLS 1.3) |
| Cloudflare (`--anywhere` only) | the viewer's IP address, the screen pictures and the input passing through (Cloudflare ends the outer TLS). Never the key: sign-in sends a one-time proof |

Cloudora writes **nothing about viewers or the screen** to disk: no database, no recording, no request log, no
history. Sessions, challenges, rate-limit counters and the last picture live in memory and vanish when Cloudora stops.
The data directory holds only: the self-signed certificate, the downloaded `cloudflared` (with `--anywhere`), its
log, and on Linux/Wayland the desktop's remote-control permission token.

## 3. Controls

### 3.1 The secret key
* `CLDR-` + 40 symbols from a 30-character alphabet (about 196 random bits, from `secrets`). It cannot be guessed;
  Cloudora keeps only its SHA-256 digest in memory and compares in constant time.
* New at every start unless `--key-file` keeps one. A key file is created with mode 0600 and refused if other users
  can read it (POSIX).
* The key is printed only to an interactive terminal, never into logs or redirected output (tested).
* Wrong keys are rate-limited per address (5 within 10 minutes → 30 s lock, doubling up to 1 hour) and in total
  (30 per minute for everyone together). Behind `--anywhere`, the address comes from `CF-Connecting-IP`, trusted
  only from loopback and only in that mode (tested, including spoofed headers).
* **The key never travels.** The sign-in page's only script (pinned in the CSP by its hash) computes
  HMAC-SHA256(SHA-256(key), `cloudora-sign-in-v1|<challenge>|<origin>`) with WebCrypto and posts just that. The
  challenge is random, bound to the browser's CSRF cookie, works once and expires after 10 minutes, so a recorded
  proof is useless again and on any other address (tested). A form that sends the key itself is refused.
  **Limit:** someone who can *change* the sign-in page in transit (e.g. Cloudflare with `--anywhere`, or anyone if
  you click through a certificate warning you did not verify) could swap the script and capture the key as it is
  typed. Compare the certificate fingerprint Cloudora prints on first use.
* **New key without stopping:** type N and press Enter in Cloudora's window (or send SIGHUP). The old key stops
  working at once, every session ends and its screen connection closes, open sign-in pages are invalidated, and a
  `--key-file` is rewritten atomically (tested).

### 3.2 Sessions
* A signed-in browser holds a random 256-bit cookie `__Host-cloudora` (Secure, HttpOnly, SameSite=Strict); Cloudora
  keeps only its SHA-256. Sign-in needs an HMAC CSRF token (key random per start) and a matching
  `Origin`.
* A session lasts at most 12 hours and ends after 60 minutes unused (never while its screen is open). At most 8 at
  once; a new sign-in replaces the longest-unused one.
* Ending a session (expiry, new key, Ctrl+C) closes its screen connection immediately (tested).

### 3.3 The screen connection (`/ws`)
* Before anything is sent, the WebSocket checks the `Origin` (cross-site WebSocket hijacking), the session cookie,
  and a token bound to that session that only Cloudora's own viewer page carries (tested: each missing piece is
  refused and the screen is not even opened).
* Messages from the viewer are small (64 KiB limit), rate-limited (600 per second), and strictly validated: known
  message types only, numbers clamped to the screen, keys only from a fixed table, text limited and stripped of
  control characters (tested with malformed input).
* One viewer controls the screen at a time; a new connection takes over and the earlier viewer is told.
* Keys and mouse buttons still held when a viewer disconnects are released on the host (tested), so a dropped
  connection never leaves Ctrl or a mouse button stuck down.
* `--view-only` ignores all input (tested), including relative motion.
* **Sound** is captured only while a signed-in viewer has it turned on, and only what the host plays (never a
  microphone). Relative-motion messages are validated like all input (ranges, sizes).
* The screen is opened only while a viewer is connected (and for 30 seconds after, for quick reconnects).

### 3.4 Reaching the host
* **Default:** HTTPS on all interfaces with a self-signed ECDSA P-256 certificate for the host's IP addresses (TLS
  1.3 only; the private key is 0600; the computer's name is not put into the certificate). Cloudora refuses to listen
  beyond loopback without HTTPS. The start-up banner shows the certificate fingerprint to compare on the first visit.
* **`--host ADDRESS`:** the same, on one address only (e.g. a VPN interface).
* **`--local`:** loopback only, plain HTTP (browsers treat `127.0.0.1` as secure).
* **`--anywhere`:** a Cloudflare quick tunnel to 127.0.0.1. `cloudflared` is used from PATH, or downloaded from its
  official GitHub release and **run only if its SHA-256 equals the digest GitHub publishes**. After starting, Cloudora
  checks the public address end to end, including that the screen WebSocket refuses anyone not signed in.
* Port forwarding exposes the sign-in page to the whole internet. That is what the key and the rate limits are for,
  but a VPN (WireGuard, Tailscale) exposes nothing.

### 3.5 Platform permissions
* **Windows:** no extra permission. Cloudora cannot see or control the secure desktop (lock screen, Ctrl+Alt+Del,
  UAC prompts); windows of programs running as administrator ignore its input unless Cloudora is also elevated. Do
  not run Cloudora as administrator unless you need that.
* **macOS:** Screen Recording and Accessibility must be allowed for the app Cloudora runs in. Cloudora checks both and
  explains; without Accessibility the viewer can only watch.
* **Linux/Wayland:** the desktop's remote-desktop portal asks someone at the computer to allow it; Cloudora asks for
  the permission to be remembered and keeps the desktop's restore token (0600) in the data directory. Delete
  `wayland-restore-token` there, or revoke it in the desktop's settings, to require approval again.
* **Linux/X11:** any program in the session can read the screen and send input (that is how X11 works).

### 3.6 The host itself
* Cloudora runs as your normal user; there is nothing to install as root. Do not run it as root.
* It starts no programs except, each with a fixed argument list and no shell: `cloudflared` (`--anywhere`) and, on
  Linux with Wayland, `gst-launch-1.0` (the picture from PipeWire) and `pw-link` (connects it when the session
  manager does not). Child processes end with Cloudora, even if it is killed outright (parent-death signal). No
  `eval`, `exec`, `pickle` or shell anywhere (enforced by a test).
* Zero-copy GPU encoding (Linux/Wayland) runs GStreamer inside Cloudora's process through the system's GStreamer
  Python bindings, with a fixed pipeline description (only numbers and the PipeWire node id are filled in).
* Portal pages run only Cloudora's own scripts (the hash-pinned sign-in script and `/static/viewer.js`), send
  `frame-ancestors 'none'`, HSTS, `no-store`, `nosniff` and a restrictive `Permissions-Policy`. There is no API, no
  admin page and no account page (tested: they return 404).

### 3.7 Cryptography
Only established primitives: the standard library `ssl` (TLS 1.3), `cryptography` (ECDSA P-256 certificates),
`hashlib`/`hmac` (SHA-256, HMAC-SHA-256), `secrets`, and WebCrypto in the browser. No custom encryption.

## 4. Checking it yourself
* `.venv/bin/python -m pytest` runs the automated tests (see TESTING.md).
* `.venv/bin/bandit -r src -ll` (static analysis) and `.venv/bin/pip-audit` (known vulnerabilities in
  dependencies).
