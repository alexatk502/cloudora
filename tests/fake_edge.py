"""A stand-in for a tunnel edge (Cloudflare quick tunnel) used by the tests: HTTP/1.1 + WebSocket reverse proxy that
adds CF-Connecting-IP / X-Forwarded-* like Cloudflare, one request per connection (or a spliced WebSocket)."""

from __future__ import annotations

import asyncio


class FakeEdge:
    def __init__(self, backend_port: int, visitor_ip: str = "203.0.113.50", rewrite_host: bool = False,
                 block_websockets: bool = False, upload_bytes_per_s: int = 0):
        self.backend_port, self.visitor_ip, self.rewrite_host = backend_port, visitor_ip, rewrite_host
        self.block_websockets = block_websockets  # behave like a school/work filter that refuses WebSocket upgrades
        self.refused_websockets = 0
        # Imitate a home connection: everything the host sends to visitors shares one upload of this many bytes/s
        # (0 = unlimited).
        self.upload_bytes_per_s = upload_bytes_per_s
        self._next_free = 0.0
        self.server = None
        self.requests: list[bytes] = []

    async def start(self, host: str = "127.0.0.1") -> "FakeEdge":
        self.server = await asyncio.start_server(self._handle, host, 0)
        return self

    @property
    def port(self) -> int:
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()

    async def _handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except Exception:  # noqa: BLE001
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        out = [lines[0]]
        upgrade = any(l.lower().startswith("upgrade:") for l in lines[1:])
        if upgrade and self.block_websockets:
            self.refused_websockets += 1
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            try:
                await writer.drain()
            finally:
                writer.close()
            return
        for l in lines[1:]:
            low = l.lower()
            if not l or low.startswith(("cf-connecting-ip:", "x-forwarded-for:", "x-forwarded-proto:")):
                continue
            if low.startswith("connection:") and not upgrade:
                continue
            if low.startswith("host:") and self.rewrite_host:
                l = f"Host: 127.0.0.1:{self.backend_port}"
            out.append(l)
        out += [f"CF-Connecting-IP: {self.visitor_ip}", f"X-Forwarded-For: {self.visitor_ip}",
                "X-Forwarded-Proto: https"] + ([] if upgrade else ["Connection: close"]) + ["", ""]
        self.requests.append(lines[0].encode())
        try:
            br, bw = await asyncio.open_connection("127.0.0.1", self.backend_port)
        except OSError:
            writer.close()
            return
        bw.write("\r\n".join(out).encode("latin-1"))

        async def pipe(r, w, upload=False):
            try:
                while data := await r.read(16384 if upload and self.upload_bytes_per_s else 65536):
                    if upload and self.upload_bytes_per_s:  # one shared uplink: chunks queue for it in turn
                        loop = asyncio.get_running_loop()
                        start = max(loop.time(), self._next_free)
                        self._next_free = start + len(data) / self.upload_bytes_per_s
                        await asyncio.sleep(self._next_free - loop.time())
                    w.write(data)
                    await w.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.gather(pipe(reader, bw), pipe(br, writer, upload=True))
