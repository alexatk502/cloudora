"""How many pictures may be on their way to the viewer at once: smooth motion without lag.

The viewer acknowledges every picture once it is drawn. From that Cloudora learns two things:
* the **base delay** - the link's round trip with no queue. It is measured only on pictures sent while nothing else
  was in flight (so they never waited behind another), keeping the fastest such sample of the last 10 seconds. If the
  link has been busy without a pause for 2 seconds, Cloudora lets it drain once to take a fresh sample.
* the **delivery rate** - how many bytes per second actually reach the viewer.

How many pictures may be in flight follows the *typical high* round trip of the last 2 seconds (its 90th
percentile), so a link with a long or jittery round trip (Wi-Fi in power-saving mode: 50-150 ms) still carries a
full frame rate. What stops a queue from building is a separate check: as long as the round trip stays within the
link's own jitter of its base delay there is no queue, and pictures flow; once the delay rises beyond that, only
what the link delivers in one base delay (plus a little for encoding the next picture) stays in flight. That check
must not use the delivery rate alone: small pictures (AV1, a still desktop) make the rate look low simply because
little is sent, and would throttle the frame rate for nothing (measured: 7 fps on a 60-80 ms Wi-Fi link).
Sizing everything from the smoothed round trip would be the opposite trap: queueing raises the round trip, a bigger
window queues more, and pictures end up seconds late.
"""

from __future__ import annotations

import math
from collections import deque

BASE_WINDOW = 10.0       # seconds over which the fastest clean round trip counts as the link's base delay
RATE_WINDOW = 1.0        # seconds of acknowledgements used to measure the delivery rate
DRAIN_EVERY = 2.0        # busy this long without a clean sample: let the link drain once to measure it
LOST_AFTER = 3.0         # an acknowledgement this late is lost: forget everything in flight
SLACK = 0.04             # seconds of extra data in flight: covers capturing and encoding the next picture
RECENT = 2.0             # seconds of round trips whose 90th percentile sizes the window
MAX_FRAMES = 20          # a round trip's worth of pictures, up to 166 ms at 120 fps (333 ms at 60)
JITTER_CAP = 0.1         # never excuse more than 100 ms of extra delay as "jitter": a real queue grows beyond it
FREE_BYTES = 24_000      # beyond the delivery-rate limit, at most this much may be in flight while no queue builds


class Flow:
    def __init__(self) -> None:
        self.in_flight: dict[int, tuple[float, int, bool]] = {}   # sequence -> (sent at, bytes, sent alone)
        self.rtt = 0.05                                           # smoothed: what the viewer experiences
        self.rttvar = 0.0                                         # its typical variation (jitter), as TCP keeps it
        self._recent: deque[tuple[float, float]] = deque()        # (when, round trip) of the last RECENT seconds
        self.base_rtt: float | None = None
        self._clean: deque[tuple[float, float]] = deque()         # (when, round trip) of pictures sent alone
        self._acked: deque[tuple[float, int]] = deque()
        self._last_clean = 0.0

    def sent(self, seq: int, size: int, now: float) -> None:
        self.in_flight[seq] = (now, size, not self.in_flight)

    def acked(self, seq: int, now: float) -> bool:
        """The viewer drew picture ``seq`` (and so every earlier one)."""
        entry = self.in_flight.pop(seq, None)
        if entry is None:
            return False
        for old in [k for k in self.in_flight if k < seq]:
            self._acked.append((now, self.in_flight.pop(old)[1]))
        sent, size, alone = entry
        sample = max(0.0, now - sent)
        self.rttvar = 0.75 * self.rttvar + 0.25 * abs(sample - self.rtt)
        self.rtt = 0.8 * self.rtt + 0.2 * sample
        self._recent.append((now, sample))
        while self._recent and self._recent[0][0] < now - RECENT:
            self._recent.popleft()
        self._acked.append((now, size))
        if alone:
            self._clean.append((now, sample))
            self._last_clean = now
        while self._clean and self._clean[0][0] < now - BASE_WINDOW:
            self._clean.popleft()
        if self._clean:
            self.base_rtt = min(s for _, s in self._clean)
        return True

    def rate(self, now: float) -> float | None:
        """Bytes per second reaching the viewer, or None until there is enough to tell."""
        while self._acked and self._acked[0][0] < now - RATE_WINDOW:
            self._acked.popleft()
        if len(self._acked) < 2:
            return None
        span = max(now - self._acked[0][0], 0.05)
        return sum(b for _, b in self._acked) / span

    def queue_delay(self) -> float:
        return max(0.0, self.rtt - (self.base_rtt if self.base_rtt is not None else self.rtt))

    def jitter(self) -> float:
        return self.rttvar

    def queueing(self, margin: float = 0.03) -> bool:
        """Is a queue building - the round trip above the base delay by more than the link's own jitter explains?"""
        return self.queue_delay() > max(margin, min(3 * self.rttvar, JITTER_CAP))

    def high_rtt(self) -> float:
        """The 90th percentile of recent round trips (the base delay until there are samples)."""
        if not self._recent:
            return self.base_rtt if self.base_rtt is not None else 0.05
        samples = sorted(s for _, s in self._recent)
        return samples[min(len(samples) - 1, int(len(samples) * 0.9))]

    def bytes_in_flight(self) -> int:
        return sum(entry[1] for entry in self.in_flight.values())

    def frames_allowed(self, fps: int) -> int:
        return max(2, min(MAX_FRAMES, math.ceil(self.high_rtt() * fps) + 1))

    def may_send(self, fps: int, now: float) -> bool:
        if not self.in_flight:
            return True
        oldest = min(entry[0] for entry in self.in_flight.values())
        if now - oldest > LOST_AFTER:
            self.in_flight.clear()
            return True
        if now - self._last_clean > DRAIN_EVERY:
            return False                 # let the link drain: the next picture measures the base delay
        if len(self.in_flight) >= self.frames_allowed(fps):
            return False
        rate = self.rate(now)
        if rate is None or self.base_rtt is None:
            return False                 # one picture at a time until the link is known
        in_flight = self.bytes_in_flight()
        if in_flight < rate * (self.base_rtt + SLACK):
            return True                  # what the link delivers in one base delay
        # Small pictures on a slow-to-answer link (AV1 over Wi-Fi): their low rate is because little is sent, not a
        # full link. While no queue builds, a little more may be in flight - capped, so big pictures on a saturated
        # link cannot hide a queue behind the jitter they cause.
        return in_flight < FREE_BYTES and not self.queueing()
