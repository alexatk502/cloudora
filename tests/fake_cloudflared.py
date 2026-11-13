#!/usr/bin/env python3
"""Test stand-in for `cloudflared tunnel --url http://127.0.0.1:PORT`: runs a FakeEdge and prints its address the way
cloudflared prints the trycloudflare address."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.fake_edge import FakeEdge  # noqa: E402


async def main():
    port = int(sys.argv[sys.argv.index("--url") + 1].rsplit(":", 1)[1])
    edge = await FakeEdge(port, block_websockets=os.environ.get("FAKE_EDGE_BLOCK_WEBSOCKETS") == "1",
                          upload_bytes_per_s=int(os.environ.get("FAKE_EDGE_UPLOAD_KBPS", "0")) * 1024).start()
    print(f"INF |  Your quick Tunnel has been created! Visit it at: http://127.0.0.1:{edge.port}", flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
