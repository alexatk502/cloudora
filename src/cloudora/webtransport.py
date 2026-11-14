"""WebTransport (HTTP/3 over QUIC, on UDP): the low-latency way to the viewer, with the WebSocket as fallback.

Why: over Wi-Fi, packets are often only late or out of order, not lost - and TCP (the WebSocket) cannot tell the
difference. Measured on a real connection: 58 of 63 retransmissions were needless, yet each one made the host's
TCP halve its sending window, until a single keyframe needed several round trips - lag spikes of hundreds of
milliseconds. QUIC runs in Cloudora itself, so Cloudora chooses how it reacts (below), and its streams are
independent: a picture being repaired never holds up sound or input (the same reason Media over QUIC exists).

* Pictures go on one server-opened stream, each framed ``u32 length | packet`` (the WebSocket's packets, unchanged).
* Sound goes as datagrams: unreliable on purpose - a lost 10 ms packet is skipped instead of delaying the rest.
* Control and input go on the viewer's own stream (framed JSON both ways), so a click never waits behind a picture.
* Congestion control: Cloudora already paces pictures by measured delay (flow.py), which notices real congestion
  long before loss does. QUIC's own window therefore reacts to loss only gently and never drops below a floor,
  instead of halving on every (often imaginary) loss as TCP does.
* The certificate: browsers accept a self-signed certificate for WebTransport when the page names its SHA-256 hash
  and it is valid for at most 14 days (``serverCertificateHashes``). Cloudora makes a fresh ECDSA one at every
  start (renewed after 6 days) and puts its hash in the viewer page, which itself came over Cloudora's HTTPS - so
  the browser connects only to this very Cloudora.

Same sign-in rules as the WebSocket: the Origin must be the portal's own, and the first message must be the
screen token of a live sign-in. Offered only where the viewer reaches Cloudora directly (not through a tunnel,
which carries no UDP); anything that fails falls back to the WebSocket within two seconds.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import struct
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

log = logging.getLogger("cloudora.webtransport")

try:  # aioquic is a dependency; without it the viewer simply uses the WebSocket
    from aioquic.asyncio import QuicConnectionProtocol, serve
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import HeadersReceived, WebTransportStreamDataReceived
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.congestion.base import QuicCongestionControl, register_congestion_control
    from aioquic.quic.events import ConnectionTerminated, ProtocolNegotiated
except Exception:  # noqa: BLE001
    QuicConnectionProtocol = object      # type: ignore[assignment,misc]
    QuicCongestionControl = object       # type: ignore[assignment,misc]
    serve = None

if TYPE_CHECKING:
    from cloudora.portal.app import Portal
    from cloudora.server import Cloudora

CERT_DAYS = 13                      # browsers allow at most 14 days for a hash-pinned certificate
RENEW_AFTER = 6 * 86400             # a running Cloudora renews it well before then
MAX_FRAME = 64 * 1024               # what the viewer may send in one message (it only sends small ones)
AUTH_SECONDS = 10                   # the viewer must present its token this quickly
MAX_DATAGRAM = 1100                 # larger sound packets (never in practice) go on the picture stream
MAX_PENDING_DATAGRAMS = 10          # sound waiting for the network beyond this is dropped, never delayed


# ------------------------------------------------------------------ congestion control
class GentleCongestionControl(QuicCongestionControl):
    """A window that grows like slow start, shrinks by 15 % (not 50 %) once per loss episode, and never falls below
    FLOOR. Real congestion is handled earlier, by flow.py's delay-based pacing of pictures."""

    # QUIC paces packets at window / round trip, so the window also sets how fast a keyframe leaves: 1 MB at 30 ms
    # is ~270 Mb/s (a 150 KB keyframe in ~5 ms instead of ~18 ms with a TCP-sized window).
    FLOOR = 512 * 1024
    START = 1024 * 1024
    CEILING = 16 * 1024 * 1024

    def __init__(self, *, max_datagram_size: int) -> None:
        super().__init__(max_datagram_size=max_datagram_size)
        self.congestion_window = self.START
        self._recovery_start = 0.0

    def on_packet_acked(self, *, now: float, packet) -> None:
        self.bytes_in_flight -= packet.sent_bytes
        if packet.sent_time > self._recovery_start:
            self.congestion_window = min(self.CEILING, self.congestion_window + packet.sent_bytes)

    def on_packet_sent(self, *, packet) -> None:
        self.bytes_in_flight += packet.sent_bytes

    def on_packets_expired(self, *, packets) -> None:
        for packet in packets:
            self.bytes_in_flight -= packet.sent_bytes

    def on_packets_lost(self, *, now: float, packets) -> None:
        latest = 0.0
        for packet in packets:
            self.bytes_in_flight -= packet.sent_bytes
            latest = max(latest, packet.sent_time)
        if latest > self._recovery_start:             # one reduction per episode, not per packet
            self._recovery_start = now
            self.congestion_window = max(self.FLOOR, int(self.congestion_window * 0.85))

    def on_persistent_congestion(self) -> None:
        self.congestion_window = self.FLOOR
        self._recovery_start = 0.0

    def on_rtt_measurement(self, *, now: float, rtt: float) -> None:
        pass


if serve is not None:
    register_congestion_control("cloudora-gentle", GentleCongestionControl)


def available() -> bool:
    return serve is not None


# ------------------------------------------------------------------ certificate
def make_certificate():
    """(certificate, private key, SHA-256 of the DER) - ECDSA P-256, valid CERT_DAYS, as WebTransport requires."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Cloudora WebTransport")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=10))
            .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    return cert, key, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest()


def _frame(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


class _Frames:
    """Splits a byte stream into ``u32 length | payload`` messages."""

    def __init__(self, limit: int = MAX_FRAME) -> None:
        self.buf = bytearray()
        self.limit = limit

    def feed(self, data: bytes) -> list[bytes] | None:
        self.buf += data
        out = []
        while len(self.buf) >= 4:
            n = struct.unpack_from("<I", self.buf)[0]
            if n > self.limit:
                return None
            if len(self.buf) < 4 + n:
                break
            out.append(bytes(self.buf[4:4 + n]))
            del self.buf[:4 + n]
        return out


# ------------------------------------------------------------------ one viewer's connection
class Channel:
    """One WebTransport session, with the interface the desktop service uses for a WebSocket."""

    def __init__(self, endpoint: "_Endpoint", session_id: int):
        self.endpoint, self.session_id = endpoint, session_id
        self.control: int | None = None        # the viewer's bidirectional stream
        self.media: int | None = None          # Cloudora's stream for pictures
        self.frames = _Frames()
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.closed = False
        self.authed = False
        self.kind = "webtransport"

    # incoming
    def incoming(self, stream_id: int, data: bytes, ended: bool) -> None:
        if self.control is None:
            self.control = stream_id
        if stream_id != self.control or self.closed:
            return
        messages = self.frames.feed(data)
        if messages is None:
            self.endpoint.abort()
            return
        for m in messages:
            if not self.authed:
                self.authed = True
                self.endpoint.gate.authenticate(self, m)
            elif not self.inbox.full():
                self.inbox.put_nowait(m)
        if ended:
            self.gone()

    def gone(self) -> None:
        if not self.closed:
            self.closed = True
            with contextlib.suppress(asyncio.QueueFull):
                self.inbox.put_nowait(None)

    # the WebSocket-like interface (desktop/service.py)
    async def send_text(self, text: str) -> None:
        self._control_send(text.encode())

    def _control_send(self, data: bytes) -> None:
        if self.closed or self.control is None:
            raise ConnectionError("the viewer is gone")
        self.endpoint._quic.send_stream_data(self.control, _frame(data))
        self.endpoint.transmit()

    async def send_bytes(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("the viewer is gone")
        quic = self.endpoint._quic
        if data[:1] == b"\x03" and len(data) <= MAX_DATAGRAM:
            if len(getattr(quic, "_datagrams_pending", ())) < MAX_PENDING_DATAGRAMS:
                self.endpoint.h3.send_datagram(self.session_id, data)
                self.endpoint.transmit()
            return
        if self.media is None:
            self.media = self.endpoint.h3.create_webtransport_stream(self.session_id, is_unidirectional=True)
        quic.send_stream_data(self.media, _frame(data))
        self.endpoint.transmit()

    async def receive_text(self) -> str:
        m = await self.inbox.get()
        if m is None:
            raise ConnectionError("the viewer left")
        return m.decode("utf-8", "replace")

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        with contextlib.suppress(Exception):
            self._control_send(json.dumps({"t": "close", "code": code}).encode())
        self.gone()
        self.endpoint.finish()


class _Endpoint(QuicConnectionProtocol):
    def __init__(self, *args: Any, gate: "WebTransportServer", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gate = gate
        self.h3: Any = None
        self.channel: Channel | None = None

    def quic_event_received(self, event) -> None:
        if isinstance(event, ProtocolNegotiated):
            if event.alpn_protocol in H3_ALPN:
                self.h3 = H3Connection(self._quic, enable_webtransport=True)
        elif isinstance(event, ConnectionTerminated):
            if self.channel is not None:
                self.channel.gone()
        if self.h3 is None:
            return
        for ev in self.h3.handle_event(event):
            if isinstance(ev, HeadersReceived):
                self._connect(ev)
            elif isinstance(ev, WebTransportStreamDataReceived):
                if self.channel is not None and ev.session_id == self.channel.session_id:
                    self.channel.incoming(ev.stream_id, ev.data, ev.stream_ended)

    def _connect(self, ev) -> None:
        h = {k.decode("latin-1"): v.decode("latin-1") for k, v in ev.headers}
        wanted = (h.get(":method") == "CONNECT" and h.get(":protocol") == "webtransport" and h.get(":path") == "/wt"
                  and self.channel is None)
        if not wanted or not self.gate.origin_ok(h.get("origin", ""), h.get(":authority", "")):
            self.h3.send_headers(ev.stream_id, [(b":status", b"403")], end_stream=True)
            self.transmit()
            return
        self.h3.send_headers(ev.stream_id, [(b":status", b"200"), (b"sec-webtransport-http3-draft", b"draft02")])
        self.transmit()
        self.channel = Channel(self, ev.stream_id)
        asyncio.get_running_loop().call_later(AUTH_SECONDS, self._auth_deadline)

    def _auth_deadline(self) -> None:
        if self.channel is not None and not self.channel.authed:
            self.abort()

    def abort(self) -> None:
        if self.channel is not None:
            self.channel.gone()
        self.finish()

    def finish(self) -> None:
        """Close the connection once what is queued (the farewell message) has gone out."""
        with contextlib.suppress(Exception):
            self._quic.close(error_code=0)
            self.transmit()


# ------------------------------------------------------------------ the server
class WebTransportServer:
    def __init__(self, app: "Cloudora", portal: "Portal"):
        self.app, self.portal = app, portal
        self.port = 0
        self._server = None
        self._config = None
        self._hash = b""
        self._made = 0.0

    def _new_certificate(self) -> None:
        cert, key, digest = make_certificate()
        self._config.certificate, self._config.private_key = cert, key
        self._hash, self._made = digest, time.time()

    async def start(self, host: str, port: int) -> bool:
        if serve is None:
            return False
        self._config = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN, max_datagram_frame_size=65536,
                                         congestion_control_algorithm="cloudora-gentle", idle_timeout=20)
        self._new_certificate()
        try:
            self._server = await serve(host, port, configuration=self._config,
                                       create_protocol=lambda *a, **k: _Endpoint(*a, gate=self, **k))
        except OSError as exc:
            log.warning("WebTransport is off (UDP port %s: %s); viewers use the WebSocket", port, exc)
            return False
        self.port = port
        return True

    def offer(self) -> str:
        """What the viewer page needs: "port,base64(sha-256 of the certificate)" - or "" when it is off."""
        if self._server is None:
            return ""
        if time.time() - self._made > RENEW_AFTER:
            self._new_certificate()
        return f"{self.port},{base64.b64encode(self._hash).decode()}"

    def origin_ok(self, origin: str, authority: str) -> bool:
        return bool(origin) and urlsplit(origin).netloc in self.portal.own_netlocs(authority)

    def authenticate(self, channel: Channel, first: bytes) -> None:
        try:
            token = str(json.loads(first.decode()).get("k", ""))
        except (ValueError, AttributeError):
            token = ""
        session = next((s for s in self.app.sessions.all()
                        if hmac.compare_digest(self.portal.screen_token(s), token)), None) if token else None
        if session is None or self.app.sessions.get(session.id) is None:
            asyncio.ensure_future(channel.close(4401))      # signed out (or never signed in): back to sign-in
            return
        asyncio.ensure_future(self._serve(channel, session))

    async def _serve(self, channel: Channel, session) -> None:
        try:
            await self.app.desktop.serve(channel, session)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001
            log.warning("WebTransport viewer failed", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await channel.close(4000)

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
