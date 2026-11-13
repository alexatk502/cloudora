"""The secret key: the only thing anyone needs to use this Cloudora.

    CLDR-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX     (40 random symbols, about 196 bits)

* A new key is made every time Cloudora starts, unless ``--key-file`` keeps one (a file only you can read).
* Cloudora keeps only its SHA-256 digest in memory and compares in constant time. 196 random bits cannot be guessed,
  so no slow hash is needed; failed attempts are still rate-limited, to keep the log and the CPU quiet.
* Anyone who has the key can use the network until Cloudora stops, or until you make a new key (press N + Enter in
  Cloudora's terminal, or send it SIGHUP), which also signs everyone out.

The key never travels. The sign-in page asks the browser to prove it knows the key: it computes
HMAC-SHA256(SHA-256(key), "cloudora-sign-in-v1|<one-time challenge>|<page origin>") with WebCrypto and sends only that.
Whatever carries the connection (Cloudflare's tunnel, a TLS-inspecting school filter, a proxy log) sees a value that
is useless a second time and useless on any other address; it cannot learn the key from it (196 random bits behind
SHA-256 and HMAC). Honest limit: someone who can CHANGE the sign-in page as it travels could replace the script and
capture the key as it is typed - this protects against recording and replay, not against an active attacker in the
middle.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import OrderedDict, deque
from pathlib import Path

ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"   # no 0/O, 1/I/L or U: easy to read out and type
PREFIX, GROUPS, GROUP_LEN = "CLDR", 8, 5
LEGACY_PREFIX = "SPDR"


def generate() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(GROUPS * GROUP_LEN))
    return PREFIX + "-" + "-".join(raw[i:i + GROUP_LEN] for i in range(0, len(raw), GROUP_LEN))


def normalise(key: str) -> str:
    k = "".join(ch for ch in key.upper() if ch.isalnum())
    for prefix in (PREFIX, LEGACY_PREFIX):      # keys made before the rename to Cloudora still work
        if k.startswith(prefix):
            return k[len(prefix):]
    return k


PROOF_CONTEXT = "cloudora-sign-in-v1"


def proof(key_digest: bytes, challenge: str, origin: str) -> str:
    """What the sign-in page's script computes in the browser (hex)."""
    return hmac.new(key_digest, f"{PROOF_CONTEXT}|{challenge}|{origin}".encode(), hashlib.sha256).hexdigest()


def valid_shape(key: str) -> bool:
    k = normalise(key)
    return len(k) == GROUPS * GROUP_LEN and all(ch in ALPHABET for ch in k)


class SecretKey:
    def __init__(self, key: str | None = None):
        self.plain = key or generate()
        if not valid_shape(self.plain):
            raise ValueError("not a Cloudora secret key")
        self._digest = hashlib.sha256(normalise(self.plain).encode()).digest()

    @classmethod
    def from_file(cls, path: Path) -> "SecretKey":
        """Reuse the key in ``path``; create it (readable only by you) if it does not exist."""
        path = Path(path).expanduser()
        if path.exists():
            if os.name == "posix" and path.stat().st_mode & 0o077:
                raise SystemExit(f"{path} can be read by other users: run  chmod 600 {path}")
            return cls(path.read_text().strip())
        key = cls()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key.plain + "\n")
        return key

    def verify(self, candidate: object) -> bool:
        """Check a typed key (used for key files and tests; the sign-in page never sends the key)."""
        if not isinstance(candidate, str) or len(candidate) > 80:
            return False
        digest = hashlib.sha256(normalise(candidate).encode()).digest()
        return hmac.compare_digest(digest, self._digest)

    def verify_proof(self, challenge: str, origin: str, candidate: object) -> bool:
        if not isinstance(candidate, str) or len(candidate) != 64:
            return False
        return hmac.compare_digest(proof(self._digest, challenge, origin), candidate.lower())

    def save(self, path: Path) -> None:
        """Write the key to ``path``, readable only by you (replaces the file atomically)."""
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(self.plain + "\n")
        os.replace(tmp, path)


class Challenges:
    """One-time sign-in challenges (memory only): each is bound to the browser's CSRF cookie, works once, and expires.
    Bounded, so a flood of sign-in page loads cannot grow memory without limit."""

    def __init__(self, ttl: float = 600, limit: int = 20000):
        self.ttl, self.limit = ttl, limit
        self._open: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def issue(self, bound_to: str) -> str:
        now = time.monotonic()
        while self._open and (len(self._open) >= self.limit or next(iter(self._open.values()))[1] < now):
            self._open.popitem(last=False)
        challenge = secrets.token_urlsafe(24)
        self._open[challenge] = (bound_to, now + self.ttl)
        return challenge

    def take(self, challenge: object, bound_to: str) -> bool:
        """True once for a valid, unexpired challenge issued to this browser; it can never be used again."""
        if not isinstance(challenge, str) or len(challenge) > 64:
            return False
        found = self._open.pop(challenge, None)
        return found is not None and hmac.compare_digest(found[0], bound_to) and found[1] >= time.monotonic()

    def clear(self) -> None:
        self._open.clear()


class AttemptLimiter:
    """Failed sign-ins: per visitor address, and for everyone together.

    A visitor locks out for ``base_lock`` seconds after ``per_source`` failures within ``window``, doubling each time
    up to ``max_lock``. If more than ``global_per_minute`` failures arrive from all addresses together, everyone waits
    a minute (an attacker with many addresses gains nothing; a person who has the key just waits)."""

    def __init__(self, per_source: int = 5, window: float = 600, base_lock: float = 30, max_lock: float = 3600,
                 global_per_minute: int = 30):
        self.per_source, self.window, self.base_lock, self.max_lock = per_source, window, base_lock, max_lock
        self.global_per_minute = global_per_minute
        self._fails: dict[str, deque[float]] = {}
        self._locks: dict[str, tuple[float, float]] = {}   # source -> (locked until, last lock length)
        self._recent: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._fails) > 10000:            # bounded memory under a flood of addresses
            self._fails.clear()
        if len(self._locks) > 10000:
            self._locks = {k: v for k, v in self._locks.items() if v[0] > now}

    def blocked(self, source: str) -> float:
        """Seconds this source must wait (0 = may try)."""
        now = time.monotonic()
        self._prune(now)
        until = self._locks.get(source, (0.0, 0.0))[0]
        wait = max(0.0, until - now)
        if len(self._recent) >= self.global_per_minute:
            wait = max(wait, 60 - (now - self._recent[0]))
        return wait

    def failure(self, source: str) -> None:
        now = time.monotonic()
        self._recent.append(now)
        fails = self._fails.setdefault(source, deque())
        fails.append(now)
        while fails and now - fails[0] > self.window:
            fails.popleft()
        if len(fails) >= self.per_source:
            last = self._locks.get(source, (0.0, 0.0))[1]
            length = min(self.max_lock, last * 2 if last else self.base_lock)
            self._locks[source] = (now + length, length)
            fails.clear()

    def success(self, source: str) -> None:
        self._fails.pop(source, None)
