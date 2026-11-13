# Installation

## Requirements

* **The host** (the computer you want to reach): Windows 10/11, macOS 11 or newer, or Linux with a desktop (X11 or
  Wayland). Python 3.11 or newer (<https://www.python.org/downloads/>; on Windows tick "Add python.exe to PATH").
* **Linux with Wayland** also needs GStreamer's PipeWire plugin, which most desktops already have:
  Fedora `sudo dnf install gstreamer1-plugins-good pipewire-gstreamer`, Debian/Ubuntu
  `sudo apt install gstreamer1.0-plugins-good gstreamer1.0-pipewire`.
* **Linux with X11** needs `libXtst` (installed with every X11 desktop).
* **GPU encoding** (optional, much lower CPU and bandwidth) is used automatically when available:
  * Windows and macOS, and Linux with X11: NVIDIA, Intel and AMD GPUs and Apple Silicon through the video library
    Cloudora installs (PyAV) - nothing extra to install beyond the normal graphics driver.
  * Linux with Wayland, zero copy (the screen never leaves graphics memory): Intel and AMD GPUs through VA-API.
    Needs GStreamer's VA plugin and GStreamer's Python bindings, which GNOME and KDE usually include: Fedora
    `sudo dnf install gstreamer1-plugins-bad-free python3-gobject` (plus `intel-media-driver` for Intel, or
    `mesa-va-drivers` for AMD), Debian/Ubuntu `sudo apt install gstreamer1.0-plugins-bad python3-gi gir1.2-gstreamer-1.0`.
  * The start-up banner's `VIDEO:` line lists what Cloudora found, best first. Set `CLOUDORA_NO_GPU=1` to turn the
    zero-copy path off.
* **Sound** works on Linux and Windows hosts (macOS cannot capture what it plays). For games, prefer a wired or
  5 GHz connection on both devices; Ctrl+Alt+M in the viewer captures the mouse for mouse-look.
* **The viewing device:** any current browser (Chrome, Edge, Firefox, Safari), on a computer, phone or tablet.
  Nothing to install.

## Start

Start Cloudora **inside the desktop session you want to share** (a terminal in that desktop; not over SSH, not as a
system service), as your normal user.

| Platform | Command |
|---|---|
| Windows | double-click `scripts\quickstart.cmd`, or in PowerShell `.\scripts\quickstart.ps1` |
| macOS, Linux | `./scripts/quickstart.sh` |

The first run creates `.venv` inside the Cloudora folder and installs Cloudora's Python packages there. Nothing outside
that folder and Cloudora's data directory changes. Every start prints:

* the address to open on the other device;
* the certificate fingerprint (default mode) to compare with the browser's certificate viewer on the first visit;
* `SCREEN:` the screen Cloudora will share (the main one), or why it cannot yet;
* the **secret key**.

## First run on each platform

* **Windows:** when Windows Defender Firewall asks, allow access on **private** networks. The browser on the other
  device warns once about Cloudora's certificate: compare the fingerprint, then continue.
* **macOS:** the first start asks for **Screen Recording**. In System Settings > Privacy & Security, turn on
  **Screen Recording** and **Accessibility** for the app you started Cloudora from (Terminal, iTerm, ...), then start
  Cloudora again. The banner says `SCREEN: NOT READY` until Screen Recording is allowed.
* **Linux with Wayland:** the first start shows the desktop's remote-control dialog: choose the screen, tick
  **remember** / **allow restoring**, and allow. After that, connections start without asking. (Cloudora asks at
  start-up on purpose, while you are at the computer.)
* **Linux with X11:** nothing to allow.

## Choose how Cloudora is reached

### Your network (default)
`quickstart` with no options listens on all of the host's network addresses with HTTPS, using a self-signed
certificate made for those addresses (kept in the data directory, so the fingerprint stays the same). Open one of
the printed `https://<address>:8743` addresses on a device on the same network.

### From anywhere, with your own VPN or port forwarding
* **VPN** (recommended): with WireGuard, Tailscale or similar on both devices, start Cloudora with
  `--host <the host's VPN address>` and open `https://<that address>:8743` from the other device. Nothing is exposed
  to the internet.
* **Port forwarding:** forward TCP port 8743 on your router to the host and open `https://<your public IP>:8743`.
  The sign-in page is then reachable by anyone on the internet; the key and the rate limits protect it.

### From anywhere, with a Cloudflare tunnel: `--anywhere`
Cloudora starts a free Cloudflare quick tunnel (no account, no domain, no router changes) and prints a public
`https://….trycloudflare.com` address with a certificate every browser trusts. `cloudflared` is used from PATH, or
downloaded once from its official GitHub release and checked against the SHA-256 digest GitHub publishes. The address
changes at every start. Cloudflare carries the connection and could see the screen pictures and input (never the
key). Some networks block `trycloudflare.com`.

### Only this computer: `--local`
Plain HTTP on `127.0.0.1`, for trying Cloudora out.

### Your own reverse proxy or tunnel
Run Cloudora with `--local` (or `--host`) and give the address viewers use with `--public-url https://…`. The proxy
must pass WebSockets (`/ws`).

## Options

| Option | Meaning |
|---|---|
| `--anywhere` / `--local` / `--host ADDR` | how Cloudora is reached (see above); default: your network, HTTPS |
| `--port N` | port (default 8743) |
| `--key-file PATH` | keep the same key across restarts (created readable only by you; N + Enter rewrites it) |
| `--view-only` | show the screen but ignore the viewer's mouse and keyboard |
| `--public-url URL` | the address viewers use, behind your own proxy or tunnel |
| `--data-dir DIR` | where the certificate etc. are kept (also `CLOUDORA_DATA_DIR`) |

While Cloudora runs: type **N** and press Enter for a new key (signs everyone out); **Ctrl+C** stops Cloudora.

## Running it in the background
Cloudora must run inside the desktop session it shares, so start it from the desktop (for example with your
desktop's "autostart" settings). Use `--key-file`: when the output is not a terminal, Cloudora does not print the key
(so it never ends up in a log), and the key file is how you read it.

## Updating and removing
* Update: `git pull` (or download again), then start again; `quickstart` reuses `.venv`. If a new version adds
  packages: `.venv/bin/pip install -e .` (Windows: `.venv\Scripts\pip install -e .`).
* Remove: delete the Cloudora folder and its data directory (see ARCHITECTURE.md, "What is stored"), and your key file
  if you made one. On Wayland, also revoke Cloudora in the desktop's remote-desktop permissions if you like.

## Troubleshooting

| What you see | What to do |
|---|---|
| `port 8743 is already in use` | An earlier Cloudora is still running: stop it (Ctrl+C in its window), or use `--port`. |
| The other device cannot open the address | Same network? Windows: allow Cloudora in the firewall (private networks). Linux: open the port in `firewalld`/`ufw`. Try the other printed addresses. |
| The browser warns about the certificate | Expected once with the default mode: compare the fingerprint Cloudora printed, then continue. |
| `SCREEN: NOT READY - … graphical desktop session` | Start Cloudora from a terminal inside the desktop, not over SSH or as a service. |
| *Waiting for someone at the computer to allow remote control* | Linux/Wayland: allow it in the dialog on the host (tick "remember"). |
| *macOS has not allowed Cloudora…* | Allow Screen Recording and Accessibility (see "First run"), then restart Cloudora. |
| *The desktop shared the screen but no picture arrived* | Linux/Wayland: install the GStreamer PipeWire plugin and PipeWire's tools (`pw-link`; Fedora `pipewire-utils`, Debian/Ubuntu `pipewire-bin`). |
| Clicks do nothing in one window (Windows) | That program runs as administrator; Windows ignores input from normal programs there. |
| Black picture on the lock screen (Windows) | Windows protects the lock screen; keep the computer unlocked while you use it remotely. |
| It feels slow over the internet | The host's upload speed is the limit. Make the browser window smaller (pictures are never larger than the window), or use a VPN instead of `--anywhere`. |
| A `--anywhere` check says **FAIL** | Stop and start again for a new address; `tunnel.log` in the data directory shows details. Some networks block Cloudflare. |
| "Too many wrong keys" | Wait the time shown. Copy the key exactly (spaces and dashes do not matter, letters are not case-sensitive). |
