# Cloudora

See and control a computer from any other device's web browser, with one secret key. Cloudora runs on the computer you
want to reach (the **host**: Windows, macOS or Linux). On the other device (a phone, a tablet, another computer) you
open an address in any browser, paste the key, and the host's screen appears. Mouse, keyboard and touch work. There
is nothing to install on the viewing device, no accounts, no database, and nothing is recorded: the screen is only
streamed live.

> **Honest summary.** Anyone with the key can do anything on the host that you could do sitting at it. Keep the key
> private, and press **N + Enter** in Cloudora's window (or stop Cloudora) the moment you think someone else has it.
> Read [docs/SECURITY.md](docs/SECURITY.md) before you share a key.

## How it works

```
 Viewing device's browser  ──HTTPS / secure WebSocket──►  Cloudora on the host  ──►  the host's screen,
 (sign in with the key)        same network, your own        sign-in, picture         mouse and keyboard
                               VPN/port-forward, or a        encoder, input
                               Cloudflare tunnel (optional)
```

There are no settings: everything adapts by itself. The page is just the screen.

* **Pictures:** video in the best codec both sides support - AV1, HEVC or H.264 - encoded on the host's GPU when it
  has one (Intel, AMD, NVIDIA, Apple) and decoded by the viewing device's video hardware (WebCodecs, in current
  Chrome, Edge, Safari and Firefox). On Linux with Wayland the screen goes from the desktop to the GPU encoder
  without ever being copied. Measured on a real 1920×1200 desktop: 54-58 fps at about 0.1-0.2 Mb/s, using under a
  fifth of one CPU core for everything. Without a GPU encoder, software H.264 still gives 40-50 fps at about 1-2
  Mb/s. Older browsers get JPEG patches of the changed areas.
* **Latency:** a picture leaves as soon as it is encoded (on the GPU path about 4 ms after the screen changed),
  only about one round trip's worth is ever on its way, so no queue builds up, and quality and size follow the
  connection's speed. Up to 120 fps when the viewing screen runs at 120/144 Hz. Pointer moves are sent at once.
* **Transport:** WebTransport (HTTP/3 over QUIC, UDP) where the browser has it, so Wi-Fi's late packets do not
  stall everything as they do over TCP; on emulated bad Wi-Fi it removed every lag spike over 100 ms. Pictures,
  sound and input travel independently. Otherwise, or through a tunnel, the WebSocket.
* **Smoothness on Wi-Fi:** like live-video players, the viewer measures how unevenly pictures arrive and holds them
  just long enough to show them at an even pace - nothing on a steady link, at most 50 ms on a jittery one.
* **Sound:** what the host plays (Opus, about 40 ms of buffer, kept in step with the picture; Linux and Windows
  hosts). It starts with your first click or key press, as browsers require.
* **Input:** mouse, wheel and keyboard (keys by position, so shortcuts behave as on the host's own keyboard). On
  touch screens: tap to click, hold to right-click, drag, two fingers to scroll.
* **Games:** press **Ctrl+Alt+M** to capture the mouse for mouse-look (relative, unaccelerated movement; Esc
  releases it). While it is captured the host switches to its lowest-latency settings: the newest picture is always
  shown at once, at a steady bitrate that follows the connection (the picture steps down in size when the bitrate
  falls, like a streaming site's quality ladder, so it stays sharp). Measured on a real 1920×1200 desktop: 56-58 fps
  at 14-21 ms. For fast games, use a wired or 5 GHz connection: Wi-Fi in power-saving mode adds 50-150 ms that no
  software can remove.
* **Sign-in:** the browser proves it knows the key with a one-time challenge; the key itself never travels.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Set up the host

Needs Python 3.11 or newer.

**Windows:** download this repository (Code > Download ZIP), then double-click `scripts\quickstart.cmd` (or run
`.\scripts\quickstart.ps1` in PowerShell). Allow Cloudora through the Windows firewall when asked (private networks).

**macOS and Linux:** in a terminal inside the desktop you want to share:

```bash
git clone https://github.com/alexatk502/cloudora.git && cd cloudora
./scripts/quickstart.sh
```

The first run installs Cloudora into `.venv` inside the folder; nothing else on the computer changes. Each start
prints the address to open, the HTTPS certificate fingerprint, and the **secret key** (`CLDR-XXXXX-…`).

| Command | Reachable from |
|---|---|
| `quickstart` (no options) | devices on the same network, by HTTPS with Cloudora's own certificate (the browser warns once) |
| `quickstart --anywhere` | any network: a free Cloudflare quick tunnel gives a public `https://…trycloudflare.com` address (no account, no router changes) |
| `quickstart --local` | this computer only (to try it out) |

From outside your network without `--anywhere`: forward port 8743 (TCP, and UDP for WebTransport) on your router, or use your own VPN (e.g.
WireGuard or Tailscale) and `--host <the VPN address>`.

**First run on each platform:**

* **macOS:** allow **Screen Recording** and **Accessibility** for the terminal app in System Settings > Privacy &
  Security, then start Cloudora again.
* **Linux with Wayland** (most current GNOME and KDE desktops): the desktop asks once to allow remote control; tick
  "remember" / "allow restoring". Cloudora needs GStreamer's PipeWire plugin (`quickstart.sh` says which package).
* **Linux with X11** and **Windows:** nothing to allow.

A new key is made at every start. To keep the same key across restarts: `--key-file <path>` (created readable only
by you). Type **N** and press Enter in Cloudora's window for a new key at any time; it signs everyone out. Ctrl+C stops
Cloudora. `--view-only` shows the screen but ignores the viewer's input. All options: `quickstart --help`. More:
[docs/INSTALLATION.md](docs/INSTALLATION.md).

## Developing

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The tests use a stand-in screen, so they never touch your real one. See [docs/TESTING.md](docs/TESTING.md) for the
real-browser test and the opt-in test against your actual screen.

## Documentation

| | |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, the picture pipeline, platform backends, source layout |
| [SECURITY.md](docs/SECURITY.md) | threat model, controls, who can see what, known limits |
| [INSTALLATION.md](docs/INSTALLATION.md) | modes, options, platform notes, firewall, troubleshooting |
| [TESTING.md](docs/TESTING.md) | what is tested and how |

## Limitations

* One key for everyone, and one viewer controls the screen at a time (a new one takes over).
* The main screen is shared (on Linux with Wayland: the one chosen in the desktop's dialog).
* No file transfer or clipboard sync. Phones and tablets work as a touch mouse; there is no on-screen keyboard.
* Sign-ins end when Cloudora stops, after 12 hours or 60 idle minutes, or with a new key (N + Enter); there is no sign-out
  button.
* Windows: the lock screen, Ctrl+Alt+Del and UAC prompts cannot be seen or used by a normal program; windows of
  programs running as administrator only respond if Cloudora runs as administrator too.
* The host's upload speed limits picture quality and frame rate over the internet.

## Licence

MIT, see [LICENSE](LICENSE). The H.264 encoder comes from PyAV, whose binary packages include FFmpeg and x264 under
their own (LGPL/GPL) licences; they are installed from PyPI, not shipped in this repository.
