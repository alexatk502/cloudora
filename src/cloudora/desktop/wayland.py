"""Linux on Wayland (KDE Plasma, GNOME, ...): the desktop's remote-desktop portal and PipeWire.

Wayland does not let programs read the screen or inject input on their own. The supported way is the
``org.freedesktop.portal.RemoteDesktop`` portal: the desktop asks someone at the computer to allow it (choose the
screen, tick "remember"/"allow restoring"). Cloudora asks for the permission to be remembered and keeps the desktop's
restore token in its data directory (one small file, readable only by you), so later connections - also remote
ones - start without anyone at the computer. Delete that file (or revoke it in the desktop's settings) to require
approval again.

The picture comes from PipeWire through ``gst-launch-1.0`` (GStreamer, part of every desktop distribution: on Fedora
``gstreamer1-plugins-good`` + ``pipewire-gstreamer``, on Debian/Ubuntu ``gstreamer1.0-plugins-good`` +
``gstreamer1.0-pipewire``). The pointer is drawn into the picture by the compositor.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess  # nosec B404 - gst-launch-1.0 only: fixed argument list, no shell
import threading
import time
from pathlib import Path

from cloudora.desktop import Backend, DesktopUnavailable, Frame, Screen, WheelAccumulator
from cloudora.desktop.keys import EVDEV_BUTTONS, Key

log = logging.getLogger("cloudora.desktop")

_BUS = "org.freedesktop.portal.Desktop"
_PATH = "/org/freedesktop/portal/desktop"
APPROVAL_SECONDS = 300          # how long to wait for someone at the computer to allow it


def _die_with_parent() -> None:
    """In the child, before gst-launch starts: end it if Cloudora ends, even if Cloudora is killed outright."""
    try:
        import ctypes
        import signal
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)    # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass


def _value(variant):
    """jeepney gives a{sv} values as (signature, value)."""
    return variant[1] if isinstance(variant, tuple) and len(variant) == 2 and isinstance(variant[0], str) else variant


class WaylandBackend(Backend):
    name = "Wayland (remote-desktop portal + PipeWire)"
    cursor_in_frames = True
    host_repeats = True

    def __init__(self, token_path: Path, status=lambda _msg: None, cancel: threading.Event | None = None):
        try:
            from jeepney import DBusAddress
            from jeepney.io.blocking import open_dbus_connection
        except ImportError as exc:  # pragma: no cover - jeepney is a Linux dependency of Cloudora
            raise DesktopUnavailable("Cloudora's D-Bus library (jeepney) is missing: reinstall Cloudora.") from exc
        self.gst = shutil.which("gst-launch-1.0")
        if not self.gst:
            raise DesktopUnavailable("GStreamer is not installed on the computer. Install it (Fedora: sudo dnf install "
                                     "gstreamer1-plugins-good pipewire-gstreamer; Debian/Ubuntu: sudo apt install "
                                     "gstreamer1.0-plugins-good gstreamer1.0-pipewire) and connect again.")
        self.token_path = token_path
        self._status = status
        self._cancel = cancel
        self._lock = threading.Lock()
        self._frame_cond = threading.Condition()
        self._frame: Frame | None = None
        self._frame_no = 0
        self._seen_no = 0
        self._ended = ""
        self._proc: subprocess.Popen | None = None
        self._data = None
        self._size = (0, 0)
        self._size_known = threading.Event()
        self._pw_name = ""
        self._pw_fd = -1
        self._start_lock = threading.Lock()
        self._stopping = False           # the raw pipeline is being stopped on purpose (the GPU takes over)
        self._wheel = WheelAccumulator()
        self.session = ""
        try:
            self.conn = open_dbus_connection(bus="SESSION", enable_fds=True)
        except Exception as exc:  # noqa: BLE001
            raise DesktopUnavailable("Cannot reach the desktop session (D-Bus). Start Cloudora from a terminal inside "
                                     "the desktop, not over SSH.") from exc
        self.rd = DBusAddress(_PATH, bus_name=_BUS, interface="org.freedesktop.portal.RemoteDesktop")
        self.sc = DBusAddress(_PATH, bus_name=_BUS, interface="org.freedesktop.portal.ScreenCast")
        try:
            self._start()
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------------ portal handshake
    def _property(self, addr, name: str):
        from jeepney import Properties
        reply = self.conn.send_and_get_reply(Properties(addr).get(name), timeout=10)
        if reply.header.message_type.name == "error":
            return None
        return _value(reply.body[0])

    def _call(self, addr, method: str, signature: str, body: tuple):
        from jeepney import new_method_call
        reply = self.conn.send_and_get_reply(new_method_call(addr, method, signature, body), timeout=15)
        if reply.header.message_type.name == "error":
            raise DesktopUnavailable(f"The desktop refused remote control ({method}: {reply.body[0] if reply.body else '?'}).")
        return reply.body

    def _request(self, addr, method: str, signature: str, args: tuple, options: dict, timeout: float = 30) -> dict:
        """Portal calls answer through a Request object's Response signal."""
        from jeepney import MatchRule, message_bus
        token = "cloudora" + secrets.token_hex(6)
        sender = self.conn.unique_name[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response", path=path)
        self.conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=10)
        with self.conn.filter(rule) as queue:
            self._call(addr, method, signature, (*args, {**options, "handle_token": ("s", token)}))
            deadline = time.monotonic() + timeout
            while True:
                if self._cancel is not None and self._cancel.is_set():
                    raise DesktopUnavailable("Cloudora is stopping.")
                try:
                    signal = self.conn.recv_until_filtered(queue, timeout=min(1.0, max(0.05, deadline - time.monotonic())))
                    break
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise DesktopUnavailable("Nobody allowed remote control on the computer in time. Someone at "
                                                 "the computer must allow it once (it is remembered after that).") from None
        code, results = signal.body
        if code == 1:
            raise DesktopUnavailable("Remote control was declined on the computer.")
        if code != 0:
            raise DesktopUnavailable(f"The desktop could not start remote control ({method} ended with code {code}).")
        return {k: _value(v) for k, v in results.items()}

    def _start(self) -> None:
        rd_version = self._property(self.rd, "version") or 1
        devices = self._property(self.rd, "AvailableDeviceTypes") or 0
        if not devices & 3:
            raise DesktopUnavailable("This desktop's portal cannot control the keyboard and mouse.")
        cursor_modes = self._property(self.sc, "AvailableCursorModes") or 1
        res = self._request(self.rd, "CreateSession", "a{sv}", (),
                            {"session_handle_token": ("s", "cloudora" + secrets.token_hex(4))})
        self.session = str(res["session_handle"])
        dev_opts: dict = {"types": ("u", 3)}                                # keyboard + pointer
        token = self._read_token()
        if rd_version >= 2:
            dev_opts["persist_mode"] = ("u", 2)                              # remember until revoked
            if token:
                dev_opts["restore_token"] = ("s", token)
        self._request(self.rd, "SelectDevices", "oa{sv}", (self.session,), dev_opts)
        self._request(self.sc, "SelectSources", "oa{sv}", (self.session,),
                      {"types": ("u", 1), "multiple": ("b", False),
                       "cursor_mode": ("u", 2 if cursor_modes & 2 else 1)})
        if not token:
            self._status("Waiting for someone at the computer to allow remote control (only needed once).")
            log.warning("remote control: allow it in the dialog on this computer (tick 'remember' / "
                        "'allow restoring' so it is not asked again)")
        res = self._request(self.rd, "Start", "osa{sv}", (self.session, ""), {}, timeout=APPROVAL_SECONDS)
        if res.get("restore_token"):
            self._write_token(str(res["restore_token"]))
        streams = res.get("streams") or []
        if not streams:
            raise DesktopUnavailable("No screen was chosen on the computer.")
        node, props = streams[0]
        props = {k: _value(v) for k, v in props.items()}
        w, h = props.get("size") or (0, 0)
        if not w or not h:
            raise DesktopUnavailable("The desktop did not say how large the screen is.")
        self.node, self.width, self.height = int(node), int(w), int(h)
        (fd,) = self._call(self.sc, "OpenPipeWireRemote", "oa{sv}", (self.session, {}))
        # Kept open while the screen is shared: the raw-picture pipeline (started when first needed) and the GPU
        # encoder (gpu.py) each connect to PipeWire through their own copy of it.
        self._pw_fd = fd.to_raw_fd() if hasattr(fd, "to_raw_fd") else int(fd)

    def _read_token(self) -> str:
        try:
            return self.token_path.read_text().strip()[:500]
        except OSError:
            return ""

    def _write_token(self, token: str) -> None:
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.token_path.with_name(self.token_path.name + ".new")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(token)
            os.replace(tmp, self.token_path)
        except OSError:
            log.warning("could not keep the desktop's permission token; it will ask again next time")

    # ------------------------------------------------------------------ picture
    def _start_pipeline(self, pw_fd: int) -> None:
        """gst-launch reads the screen from PipeWire and writes raw BGRx pictures into a pipe. Its standard output
        (-v) only reports the negotiated picture size, so pixels and messages never mix."""
        self._pw_name = "cloudora-capture-" + secrets.token_hex(4)
        data_r, data_w = os.pipe()
        try:
            import fcntl
            fcntl.fcntl(data_w, 1031, 4 << 20)       # F_SETPIPE_SZ: room for a whole picture (best effort)
        except (ImportError, OSError):
            pass
        try:
            self._proc = subprocess.Popen(  # nosec B603 - fixed program and arguments
                [self.gst, "-v", "pipewiresrc", f"fd={pw_fd}", f"path={self.node}", "always-copy=true",
                 f"client-name={self._pw_name}", f"stream-properties=props,node.name=(string){self._pw_name}",
                 "!", "videoconvert", "!", "video/x-raw,format=BGRx", "!", "fdsink", f"fd={data_w}", "sync=false"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(pw_fd, data_w),
                preexec_fn=_die_with_parent)  # noqa: PLW1509 - only sets the parent-death signal
        finally:
            os.close(data_w)
        self._data = os.fdopen(data_r, "rb", buffering=0)
        threading.Thread(target=self._read_messages, name="cloudora-gst-messages", daemon=True).start()
        threading.Thread(target=self._read_frames, name="cloudora-pipewire", daemon=True).start()
        if not self._size_known.wait(1.5):
            self._link(self._pw_name)  # some session managers (seen: WirePlumber 0.5 with KWin) never link it
            if not self._size_known.wait(5):
                raise DesktopUnavailable("The desktop shared the screen but no picture arrived. Make sure PipeWire "
                                         "and its tools are installed (pw-link) and try again.")

    def _link(self, name: str) -> None:
        """Connect the screen's PipeWire stream to the consumer called ``name`` (when the session manager does not)."""
        pw_link = shutil.which("pw-link")
        if not pw_link:
            return
        subprocess.run([pw_link, str(self.node), name],  # nosec B603 - fixed program and arguments
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)

    # ------------------------------------------------------------------ GPU encoding (zero copy, see gpu.py)
    def gpu_codecs(self) -> list[str]:
        from cloudora.desktop import gpu
        return gpu.available_kinds()

    def open_gpu_encoder(self, kind: str, preset: str, fps: int):
        """The screen's stream can feed only one consumer format at a time (raw pictures in normal memory, or
        DMA-BUF for the GPU), so the raw pipeline stops first; grab() starts it again if needed later."""
        from cloudora.desktop import gpu
        self._stop_pipeline()
        return gpu.GstEncoder(os.dup(self._pw_fd), self.node, kind, preset, fps, link=self._link)

    def _stop_pipeline(self) -> None:
        with self._start_lock:
            proc = self._proc
            if proc is None:
                return
            self._stopping = True
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            if self._data is not None:
                try:
                    self._data.close()
                except OSError:
                    pass
            time.sleep(0.05)
            with self._frame_cond:
                self._proc, self._data, self._frame = None, None, None
                self._size_known = threading.Event()
                self._stopping = False

    def _read_messages(self) -> None:
        """-v prints the negotiated caps; the fdsink's input caps give the size of every picture that follows."""
        import re
        pattern = re.compile(rb"GstFdSink:.*caps = video/x-raw.*?width=\(int\)(\d+), height=\(int\)(\d+)")
        for line in self._proc.stdout:
            m = pattern.search(line)
            if m:
                size = (int(m.group(1)), int(m.group(2)))
                if self._size_known.is_set() and size != self._size:
                    with self._frame_cond:      # the screen's resolution changed: Cloudora reconnects
                        self._ended = "The screen's resolution changed."
                        self._frame_cond.notify_all()
                    return
                self._size = size
                self._size_known.set()

    def _read_frames(self) -> None:
        try:
            while not self._size_known.wait(1):
                if self._proc is None or self._proc.poll() is not None:
                    raise EOFError
            w, h = self._size
            size = w * h * 4
            while True:
                buf = bytearray(size)
                view, got = memoryview(buf), 0
                while got < size:
                    n = self._data.readinto(view[got:])
                    if not n:
                        raise EOFError
                    got += n
                with self._frame_cond:
                    self._frame = Frame(w, h, buf)
                    self._frame_no += 1
                    self._frame_cond.notify_all()
        except (EOFError, OSError, ValueError, AttributeError):
            err = b""
            try:
                proc = self._proc
                err = proc.stderr.read(2000) if proc is not None and proc.stderr and proc.poll() is not None else b""
            except (OSError, ValueError):
                pass
            with self._frame_cond:
                if self._stopping:
                    return
                if not self._ended:
                    self._ended = ("Screen sharing stopped on the computer." if not err.strip() else
                                   "The screen picture stopped: " + err.decode(errors="replace").strip()[-300:])
                self._frame_cond.notify_all()

    def screens(self) -> list[Screen]:
        w, h = self._size if any(self._size) else (self.width, self.height)
        return [Screen(0, f"Shared screen ({w}×{h})", w, h)]

    def grab(self, screen: int, timeout: float) -> Frame | None:
        if self._proc is None and not self._ended:
            with self._start_lock:             # raw pictures only when needed (not while the GPU encodes)
                if self._proc is None:
                    self._start_pipeline(self._pw_fd)
        with self._frame_cond:
            if self._frame_no == self._seen_no and not self._ended:
                self._frame_cond.wait(timeout)
            if self._ended:
                raise DesktopUnavailable(self._ended)
            if self._frame_no == self._seen_no:
                return None
            self._seen_no = self._frame_no
            return self._frame

    # ------------------------------------------------------------------ input (fire and forget)
    def _notify(self, method: str, signature: str, *args) -> None:
        from jeepney import new_method_call
        from jeepney.low_level import MessageFlag
        msg = new_method_call(self.rd, method, "oa{sv}" + signature, (self.session, {}, *args))
        msg.header.flags |= MessageFlag.no_reply_expected
        with self._lock:
            self.conn.send(msg)

    def move(self, screen: int, fx: float, fy: float) -> None:
        self._notify("NotifyPointerMotionAbsolute", "udd", self.node, fx * self.width, fy * self.height)

    def nudge(self, dx: int, dy: int) -> None:
        self._notify("NotifyPointerMotion", "dd", float(dx), float(dy))

    def button(self, button: int, down: bool) -> None:
        self._notify("NotifyPointerButton", "iu", EVDEV_BUTTONS[button], 1 if down else 0)

    def wheel(self, dx: int, dy: int) -> None:
        sx, sy = self._wheel.add(dx, dy)
        if sy:
            self._notify("NotifyPointerAxisDiscrete", "ui", 0, sy)
        if sx:
            self._notify("NotifyPointerAxisDiscrete", "ui", 1, sx)

    def key(self, key: Key, down: bool) -> None:
        self._notify("NotifyKeyboardKeycode", "iu", key.evdev, 1 if down else 0)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._data is not None:
            try:
                self._data.close()
            except OSError:
                pass
        if self._pw_fd >= 0:
            try:
                os.close(self._pw_fd)
            except OSError:
                pass
            self._pw_fd = -1
        if self.session:
            try:
                from jeepney import DBusAddress, new_method_call
                addr = DBusAddress(self.session, bus_name=_BUS, interface="org.freedesktop.portal.Session")
                self.conn.send_and_get_reply(new_method_call(addr, "Close"), timeout=5)
            except Exception:  # noqa: BLE001 - closing must always finish
                pass
            self.session = ""
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
