"""A minimal WebTransport viewer (aioquic client) for tests: opens a session the way the browser does, sends JSON on
its own stream, and collects framed pictures and sound datagrams."""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import struct

from aioquic.asyncio import QuicConnectionProtocol, connect
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DatagramReceived, HeadersReceived, WebTransportStreamDataReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated

from cloudora.webtransport import _Frames


class WTViewer(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.h3 = H3Connection(self._quic, enable_webtransport=True)
        self.status: asyncio.Future = asyncio.get_running_loop().create_future()
        self.session = self.control = None
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.frames: dict[int, _Frames] = {}

    async def open(self, authority: str, origin: str) -> int:
        self.session = self._quic.get_next_available_stream_id()
        self.h3.send_headers(self.session, [
            (b":method", b"CONNECT"), (b":scheme", b"https"), (b":authority", authority.encode()),
            (b":path", b"/wt"), (b":protocol", b"webtransport"), (b"origin", origin.encode()),
            (b"sec-webtransport-http3-draft02", b"1")])
        self.transmit()
        status = await asyncio.wait_for(self.status, 5)
        if status == 200:
            self.control = self.h3.create_webtransport_stream(self.session, is_unidirectional=False)
        return status

    def send_json(self, obj) -> None:
        data = json.dumps(obj).encode()
        self._quic.send_stream_data(self.control, struct.pack("<I", len(data)) + data)
        self.transmit()

    def quic_event_received(self, event) -> None:
        for ev in self.h3.handle_event(event):
            if isinstance(ev, HeadersReceived) and not self.status.done():
                self.status.set_result(int(dict(ev.headers)[b":status"]))
            elif isinstance(ev, WebTransportStreamDataReceived):
                frames = self.frames.setdefault(ev.stream_id, _Frames(limit=1 << 30))
                for m in frames.feed(ev.data) or []:
                    self.inbox.put_nowait(json.loads(m) if ev.stream_id == self.control else m)
            elif isinstance(ev, DatagramReceived):
                self.inbox.put_nowait(ev.data)
        if isinstance(event, ConnectionTerminated):
            self.inbox.put_nowait(None)

    async def next(self, timeout: float = 3.0):
        m = await asyncio.wait_for(self.inbox.get(), timeout)
        if m is None:
            raise ConnectionError("closed")
        return m

    async def until(self, pred, timeout: float = 5.0):
        async with asyncio.timeout(timeout):
            while True:
                m = await self.next(timeout)
                if pred(m):
                    return m


@contextlib.asynccontextmanager
async def wt_viewer(host: str, port: int, origin: str | None = None):
    cfg = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, max_datagram_frame_size=65536,
                            verify_mode=ssl.CERT_NONE)
    async with connect(host, port, configuration=cfg, create_protocol=WTViewer) as v:
        v.authority = f"{host}:{port}"
        v.origin = origin or f"https://{host}:{port}"
        yield v
