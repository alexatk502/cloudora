"""Screen pictures -> as few bytes as possible, quickly.

Only the parts of the screen that changed since the last picture are sent, each as a small JPEG. A desktop is mostly
still (a blinking cursor, one window scrolling), so this usually sends a few kilobytes instead of a whole screen.
Changed areas are found band by band (16 rows at a time) and widened to 16-pixel boundaries, which line up with JPEG's
own blocks so the patches join without visible seams.

Wire format of one picture message (little-endian), decoded by the viewer page:
    u8 1 | u32 sequence | u16 width | u16 height | u16 count | count x (u16 x, u16 y, u16 w, u16 h, u32 len, JPEG)
"""

from __future__ import annotations

import io
import os
import struct
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from cloudora.desktop import Frame

BAND = 16
ALIGN = 16
FULL_AREA = 0.55          # above this share of the screen, one whole picture is cheaper than many patches
MAX_RECTS = 48
# Several changed areas are compressed at once on different cores (as KasmVNC does); Pillow releases Python's lock
# while compressing, so this is real parallelism.
_POOL = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1), thread_name_prefix="cloudora-jpeg")

Rect = tuple[int, int, int, int]


def _first_diff(a: bytes, b: bytes) -> int:
    """Index of the first differing byte (a != b, same length). Binary search: compares about len(a) bytes."""
    lo, hi = 0, len(a)
    while hi - lo > 64:
        mid = (lo + hi) // 2
        if a[lo:mid] == b[lo:mid]:
            lo = mid
        else:
            hi = mid
    for i in range(lo, hi):
        if a[i] != b[i]:
            return i
    return hi


def _last_diff(a: bytes, b: bytes) -> int:
    lo, hi = 0, len(a)
    while hi - lo > 64:
        mid = (lo + hi) // 2
        if a[mid:hi] == b[mid:hi]:
            hi = mid
        else:
            lo = mid
    for i in range(hi - 1, lo - 1, -1):
        if a[i] != b[i]:
            return i
    return lo


def changed_rects(prev: bytes, cur: bytes, width: int, height: int, bpp: int = 3) -> list[Rect]:
    """Rectangles (x, y, w, h) covering every pixel that differs between two pictures of the same size."""
    stride = width * bpp
    bands: list[list[int]] = []
    for y0 in range(0, height, BAND):
        y1 = min(height, y0 + BAND)
        a, b = y0 * stride, y1 * stride
        if cur[a:b] == prev[a:b]:
            continue
        left, right = stride, -1
        for y in range(y0, y1):
            r0 = y * stride
            rc, rp = cur[r0:r0 + stride], prev[r0:r0 + stride]
            if rc == rp:
                continue
            if left > 0:
                left = min(left, _first_diff(rc, rp))
            if right < stride - 1:
                right = max(right, _last_diff(rc, rp))
        x0 = (left // bpp) // ALIGN * ALIGN
        x1 = min(width, -(-(right // bpp + 1) // ALIGN) * ALIGN)
        if bands and bands[-1][3] == y0 and not (x1 < bands[-1][0] - 64 or x0 > bands[-1][2] + 64):
            last = bands[-1]                               # touching the band above: grow it
            last[0], last[2], last[3] = min(last[0], x0), max(last[2], x1), y1
        else:
            bands.append([x0, y0, x1, y1])
    rects = [(x0, y0, x1 - x0, y1 - y0) for x0, y0, x1, y1 in bands]
    if len(rects) > MAX_RECTS:
        x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
        x1, y1 = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
        rects = [(x0, y0, x1 - x0, y1 - y0)]
    return rects


def jpeg(img: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    # 4:4:4 keeps coloured text crisp at high quality; 4:2:0 is a third smaller for moving pictures.
    img.save(out, "JPEG", quality=quality, subsampling=0 if quality >= 85 else 2, optimize=False)
    return out.getvalue()


def pack(seq: int, size: tuple[int, int], patches: list[tuple[int, int, int, int, bytes]]) -> bytes:
    parts = [struct.pack("<BIHHH", 1, seq & 0xFFFFFFFF, size[0], size[1], len(patches))]
    for x, y, w, h, data in patches:
        parts.append(struct.pack("<HHHHI", x, y, w, h, len(data)))
        parts.append(data)
    return b"".join(parts)


class Encoder:
    """Keeps the last picture sent, so the next one only carries what changed."""

    def __init__(self) -> None:
        self.prev: bytes | None = None
        self.prev_raw = None
        self.size = (0, 0)
        self.full_next = True

    def refresh(self) -> None:
        """Send the whole screen next time (new viewer, new settings, or to sharpen after low-quality patches)."""
        self.full_next = True

    def encode(self, frame: Frame, max_w: int, max_h: int, quality: int
               ) -> tuple[tuple[int, int], list[tuple[int, int, int, int, bytes]]] | None:
        """The patches to send for ``frame`` scaled to fit max_w x max_h, or None if nothing changed."""
        scale = min(1.0, max_w / frame.width, max_h / frame.height)
        size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
        if not self.full_next and size == self.size and self.prev_raw is not None and frame.data == self.prev_raw:
            return None        # an idle screen costs one memory compare, not a conversion
        self.prev_raw = frame.data
        img = Image.frombuffer("RGB", (frame.width, frame.height), frame.data, "raw", "BGRX", frame.stride, 1)
        if size != (frame.width, frame.height):
            img = img.resize(size, Image.Resampling.BILINEAR, reducing_gap=2.0)
        cur = img.tobytes()
        if self.full_next or self.prev is None or size != self.size:
            rects: list[Rect] = [(0, 0, size[0], size[1])]
            self.full_next = False
        else:
            if cur == self.prev:
                return None
            rects = changed_rects(self.prev, cur, size[0], size[1])
            if sum(w * h for _, _, w, h in rects) > FULL_AREA * size[0] * size[1]:
                rects = [(0, 0, size[0], size[1])]
        self.prev, self.size = cur, size
        crops = [img.crop((x, y, x + w, y + h)) for x, y, w, h in rects]
        datas = list(_POOL.map(jpeg, crops, [quality] * len(crops))) if len(crops) > 1 else [jpeg(crops[0], quality)]
        return size, [(x, y, w, h, data) for (x, y, w, h), data in zip(rects, datas)]
