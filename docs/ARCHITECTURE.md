# Architecture

Cloudora is one Python process on the host computer: a small web server (FastAPI on uvicorn) with a sign-in page, a
viewer page and one WebSocket, plus a desktop backend for the host's platform.

## Components

| Part | Source | Job |
|---|---|---|
| Entry point | `__main__.py` | options, start-up banner, the terminal's N + Enter (new key) |
| Server | `server.py` | wires everything together, runs uvicorn, clean shutdown |
| Secret key | `secret.py` | the key, one-time sign-in challenges, wrong-key rate limits |
| Sessions | `sessions.py` | signed-in browsers, in memory only |
| Portal | `portal/app.py`, `portal/pages.py` | the sign-in page, the viewer page (just the screen), the `/ws` WebSocket |
| Viewer | `portal/viewer.js` | the browser side: draws pictures, plays sound, sends mouse/touch/keyboard; no settings |
| Desktop service | `desktop/service.py` | one viewer at a time, capture loop, automatic quality, input |
| Sound | `desktop/audio.py` | what the host plays, as Opus (Linux: PipeWire + GStreamer; Windows: WASAPI loopback) |
| Flow control | `desktop/flow.py` | how many pictures may be in flight (base delay, delivery rate) |
| Video encoders | `desktop/video.py` | encoder plan (best first), GPU encoders via PyAV, software x264/VP9/AV1 |
| Zero-copy GPU | `desktop/gpu.py` | Linux/Wayland: PipeWire DMA-BUF → VA-API convert/scale → VA-API AV1/HEVC/H.264 |
| Picture encoder | `desktop/frames.py` | fallback: finds changed areas, encodes them as JPEG patches |
| Backends | `desktop/windows.py`, `macos.py`, `x11.py`, `wayland.py` | read the screen, inject input |
| Key table | `desktop/keys.py` | browser key codes → Linux evdev, Windows scancodes, macOS keycodes |
| HTTPS | `tls.py` | the self-signed certificate for the host's addresses |
| Tunnel | `anywhere.py` | optional Cloudflare quick tunnel (`--anywhere`) |

## Flow

1. The viewer opens the address and signs in (`/login`): the page's script turns the typed key into a one-time
   HMAC proof; Cloudora checks it and sets the session cookie.
2. `/` serves the viewer page, which loads `/static/viewer.js` and opens the WebSocket `/ws`. Its first message is a
   token bound to the session; Cloudora has already checked the Origin and the cookie.
3. Cloudora opens the main screen (the platform backend) and sends `hello` (which codecs it can send, whether sound
   is available, whether the pointer is drawn into the pictures, whether the host repeats held keys).
4. The capture loop runs on its own thread: wait for the next picture (`grab`), encode it, send it.
   Input runs on a second thread, so a click never waits behind a picture being encoded.

## The picture pipeline (low latency, smooth motion)

* **Encoder plan** (`video.py`, probed once at start-up and shown in the banner). Best first: (1) zero-copy GPU
  encoding on Linux/Wayland (`gpu.py`): AV1, HEVC; (2) GPU encoders through PyAV on any OS - Intel Quick Sync first
  (it compressed desktops 2-3x better than NVENC here), then NVIDIA NVENC, AMD AMF, Apple VideoToolbox; (3) software
  x264 for H.264 (its bitrate cap follows the link live), then GPU H.264, VP9 and AV1 in software. The viewer reports
  which codecs its browser decodes (WebCodecs) and Cloudora picks the first match. If a GPU encoder fails
  mid-session, the viewer continues with the next entry.
* **Zero copy on Linux/Wayland** (`gpu.py`): the desktop hands the screen to GStreamer as DMA-BUF; `vapostproc`
  converts and scales it on the GPU; `vaav1enc`/`vah265enc`/`vah264enc` encode; Python only receives the compressed
  picture. A one-picture leaky queue keeps the encoder on the newest screen. Quality (quantiser) and size change
  live without restarting; keyframes are requested with a force-key-unit event. Measured on the real screen:
  54-58 fps, 11-14 ms round trip, 73-152 kb/s, 18.5 % of one CPU core for the whole Cloudora process - against
  1.5-1.8 cores and 0.3-1.8 Mb/s for software H.264.
* **Settings from game-streaming practice** (Sunshine/Moonlight; ideas only, their code is GPL): no B-frames, one
  reference frame, keyframes only on request (no periodic or scene-change keyframes), a rate-control buffer of one
  picture (`bitrate / fps`) so no picture is much larger than the next, the encoders' low-delay modes (NVENC
  ultra-low-latency tuning with `zerolatency` and `delay 0`; Quick Sync `low_delay_brc` and `async_depth 1`), forced
  IDR, encoder probing with fallback, and hardware decoding on the viewer.
* **Software H.264** (x264 `superfast` + `zerolatency`, ~10 ms per 1920×1200 picture): constant quality under a
  bitrate cap that follows the link; after a change the still picture is encoded a few more times to sharpen it.
  Software AV1 and VP9 compress better in benchmarks but take 2-5x longer per picture on fast motion, which live
  gave fewer frames and worse results (Sunshine keeps software AV1 off for the same reason).
* **JPEG patches** (`frames.py`, fallback for browsers without WebCodecs H.264). Each picture is compared with the
  previous one in bands of 16 rows (one memory compare per band); changed columns are found by binary search,
  widened to 16-pixel boundaries (JPEG's own blocks, so patches join without seams), and touching bands merged.
  Above 55 % of the screen one whole picture is sent instead. An unchanged screen costs one memory compare.
* **Scaled to the viewer.** The viewer reports its window size in device pixels; pictures are never larger.
* **Pipelined, never queued** (`desktop/flow.py`). Each picture has a sequence number; the viewer acknowledges it
  once drawn. From the acknowledgements Cloudora measures the link's *base delay* (only from pictures sent while
  nothing else was in flight, so queueing never inflates it; if the link stays busy for 2 s it drains once to
  re-measure), its *jitter* (as TCP does) and its *delivery rate*. Up to 10 pictures may be in flight, as many as fit
  in the recent 90th-percentile round trip at the frame rate - so slow-to-answer Wi-Fi (50-150 ms, power saving)
  still carries a full frame rate. A queue is prevented separately: beyond what the link delivers in one base delay
  (+40 ms), at most 24 KB more may be in flight, and only while the round trip stays within the link's own jitter
  (capped at 100 ms). Measured, AV1 on the GPU: 56 fps at 4 ms, 52 fps at a steady 60 ms, 49 fps at a jittery
  40-150 ms; on a saturated 2 Mb/s link 42-46 fps at full resolution. (Sizing by delivery rate alone throttled small
  AV1 pictures on a 60-80 ms Wi-Fi link to ~7 fps; sizing by the smoothed round trip lets queues grow to seconds.)
* **Paced on the viewer** (as live-video players and WebRTC do). Each video picture carries the host's time. The
  viewer measures how unevenly pictures arrive (95th percentile over 2 s) and holds each decoded picture just long
  enough to show them at the pace they were made, one per screen refresh: 0 ms on a steady link, at most 50 ms, and
  none while the mouse is captured. Sound is buffered by the same amount so they stay in step. The current buffer
  is in the page's `data-smoothing` attribute (browser developer tools).
* **Automatic quality.** Congestion means a queue is building (round trip above the base delay); a keyframe's own
  drain time (its size / link speed) is not counted. Zero-copy GPU: the quantiser rises a notch at a time, and the
  picture shrinks on the GPU only after that. x264: the bitrate cap follows the measured delivery rate. Other
  encoders keep their settings per stream, so Auto shrinks the picture only when congestion persists. JPEG: quality
  (down to 40), then resolution (down to half). Everything goes back up once the link is clear.
* **Decoded in parallel, drawn in order** (JPEG). The viewer starts decoding every patch of a picture at once
  (`createImageBitmap`) and draws them in order on a canvas.
* **Input first.** Pointer moves leave at once (at most one per 8 ms) rather than once per screen refresh; input runs
  on its own thread on the host. WebSocket compression is off (video and JPEG do not compress further).

Wire format (binary, little-endian). Video: `u8 2 | u32 seq | u16 width | u16 height | u8 flags (1 = keyframe) |
u32 host time in ms (when the picture was ready; the viewer paces by it) | [keyframe: u8 n, codec name such as avc1.640032] | bitstream`. Sound: `u8 3 | u32 seq | Opus packet`. JPEG: `u8 1 | u32 seq | u16 width | u16 height |
u16 n | n × (u16 x, u16 y, u16 w, u16 h, u32 length, JPEG bytes)`. Everything else is small JSON: `hello`, `info`, `err`, `bye`, `cur` (pointer
position when it is not drawn into the pictures) from Cloudora; `ack`, `cfg`, `ref`, `m` (move), `r` (relative
move), `b` (button), `w` (wheel), `k` (key), `rel` (release all) from the viewer.

## Sound and games

* **Sound** (`audio.py`): what the host plays, Opus in 10 ms packets at 128 kb/s. Linux: PipeWire captures the
  speakers' monitor and GStreamer encodes; Windows: WASAPI loopback (`soundcard`) and PyAV's Opus. Not
  flow-controlled (it is small) but never allowed to lag: queued packets beyond ~200 ms are dropped on the host. The
  viewer starts it with the first click or key press (browsers require one) and plays with a ~40 ms cushion plus
  the picture's buffer. macOS cannot capture its own output without a virtual audio device.
* **Mouse capture** (Ctrl+Alt+M in the viewer; Esc releases it). The viewer captures the pointer (Pointer Lock,
  `unadjustedMovement` where the browser allows) and sends relative motion, coalesced to at most one message per
  4 ms; the host injects it as relative motion (Wayland portal `NotifyPointerMotion`, Windows `SendInput` without
  ABSOLUTE, macOS Quartz events with delta fields, X11 `XTestFakeRelativeMotionEvent`) - what games read for
  mouse-look. While captured, the viewer shows every picture at once and the GPU encoder switches to a constant
  bitrate (start 8 Mb/s, one-picture rate buffer) that congestion control moves between 1.5 and 20 Mb/s, with the
  picture's size following the bitrate in steps (100 / 75 / 50 %, enough bits per pixel at each, like a streaming
  site's quality ladder). Measured on the real screen: 56-58 fps, 14-21 ms, 5-7 Mb/s.

## Ideas adopted from other remote-desktop and game-streaming projects

Ideas only - no code (several are GPL/AGPL; Cloudora stays MIT).

| Project | Adopted | Not adopted (and why) |
|---|---|---|
| Sunshine / Moonlight | GPU encoders with low-delay settings, keyframes on request only, one-picture rate buffer, encoder probing with fallback, hardware decoding | UDP + FEC transport (browsers cannot open raw UDP; WebRTC would be the route) |
| Selkies | constant bitrate with congestion control for games, Opus sound in 10 ms frames, pointer lock with raw motion | uinput gamepads and on-page statistics (built, then left out to keep the viewer to just the screen); "paint-over" (re-encode a still screen sharper): on VA-API every quality change restarts the stream with a keyframe, so a still screen is sharpened only when congestion ends |
| KasmVNC | multi-threaded rectangle encoding (JPEG fallback), relative "game pointer" mode | per-region dynamic JPEG/WebP quality: the JPEG path is only a fallback now that browsers decode video |
| RustDesk, neko | (overviews only: hardware codecs, WebRTC) | |
| Live platforms (Twitch, YouTube Live, WebRTC players) | timestamps on every picture and a viewer-side adaptive smoothing buffer that shows pictures at the pace they were made (0 ms on a steady link, at most 50 ms, none while the mouse is captured); variable-frame-rate timestamps for the encoders, with x264's per-picture budget sized to the time each picture covers; a resolution ladder for games (enough bits per pixel at every bitrate) | segments, CDNs and 2-second keyframes (HLS): they buy scale with seconds of delay; B-frames and look-ahead: better compression for a further frame or more of delay; server-side transcoding and multi-rendition simulcast: one viewer at a time, and the host already encodes the one rendition that viewer needs |

Note on the GPU path: VA-API encoders restart their stream (a keyframe) on any quality or bitrate change, so they
change at most every 3 seconds and only on sustained congestion; brief dips are absorbed by flow
control (a few pictures fewer), which costs nothing.

## Platform backends

| Platform | Picture | Input | Pointer |
|---|---|---|---|
| Windows | `mss` (GDI), DPI-aware | `SendInput` (scancodes) | position sent, drawn by the viewer |
| macOS | `mss` (Quartz) | Quartz events (`CGEventPost`) | position sent |
| Linux X11 | `mss` (XGetImage/XShm) | XTest (`libXtst`) | drawn into the picture |
| Linux Wayland | xdg-desktop-portal ScreenCast + PipeWire via `gst-launch-1.0` | xdg-desktop-portal RemoteDesktop (D-Bus, `jeepney`) | drawn into the picture by the compositor |

On Wayland, Cloudora asks the portal for a remote-desktop session that the desktop remembers (`persist_mode` 2) and
stores the restore token, so later connections start without a dialog. Pictures arrive only when the screen changes;
the newest one is kept so a new viewer, a refresh or a settings change can be served at once.

## What is stored

Nothing about viewers or the screen. On disk, in the data directory (mode 0700): `tls/` (certificate), `bin/`
(`cloudflared`, `--anywhere` only), `tunnel.log`, and on Wayland `wayland-restore-token`. Default location:
`%LOCALAPPDATA%\cloudora` (Windows), `~/Library/Application Support/cloudora` (macOS),
`$XDG_DATA_HOME/cloudora` or `~/.local/share/cloudora` (Linux); `--data-dir` or `CLOUDORA_DATA_DIR` change it.

## Source layout

```
src/cloudora/
  __main__.py  server.py  config.py  secret.py  sessions.py  tls.py  anywhere.py  errors.py
  desktop/  __init__.py (interface, backend choice)  service.py  frames.py  keys.py
            flow.py  video.py  gpu.py  audio.py  mss_capture.py  windows.py  macos.py  x11.py  wayland.py
  portal/   app.py  pages.py  viewer.js
tests/      unit, WebSocket, real-process and real-browser tests (see TESTING.md)
scripts/    quickstart.sh (Linux, macOS)  quickstart.ps1 / quickstart.cmd (Windows)
```
