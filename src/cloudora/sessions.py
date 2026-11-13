"""Signed-in browsers, in memory only. A restart signs everyone out; only SHA-256 digests of the cookies are kept."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(eq=False)
class Session:
    id: str
    token_hash: str
    created_at: float
    expires_at: float
    last_seen: float = field(default_factory=time.time)
    viewers: int = 0          # open screen connections: a session in use never counts as idle

    def touch(self) -> None:
        self.last_seen = time.time()


class Sessions:
    def __init__(self, hours: float = 12, idle_minutes: int = 60, max_sessions: int = 32):
        self.ttl = hours * 3600
        self.idle = idle_minutes * 60
        self.max_sessions = max_sessions
        self.on_end: list = []        # callbacks(session): open screen connections close
        self._by_id: dict[str, Session] = {}
        self._by_hash: dict[str, Session] = {}

    def resolve(self, token: object) -> Session | None:
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return None
        h = hash_token(token)
        session = self._by_hash.get(h)
        if session is None or not hmac.compare_digest(session.token_hash, h):
            return None
        if self._expired(session, time.time()):
            self.end(session.id, "expired")
            return None
        session.touch()
        return session

    def get(self, session_id: str) -> Session | None:
        session = self._by_id.get(session_id)
        if session is not None and self._expired(session, time.time()):
            self.end(session.id, "expired")
            return None
        return session

    def all(self) -> list[Session]:
        return list(self._by_id.values())

    def create(self) -> tuple[str, Session]:
        """A new sign-in. At the limit the longest-unused session makes room (the person holding the key wins)."""
        self.sweep()
        while len(self._by_id) >= self.max_sessions:
            self.end(min(self._by_id.values(), key=lambda s: s.last_seen).id, "replaced by a newer sign-in")
        token = new_token()
        now = time.time()
        session = Session(id=secrets.token_urlsafe(9), token_hash=hash_token(token), created_at=now,
                          expires_at=now + self.ttl)
        self._by_id[session.id] = session
        self._by_hash[session.token_hash] = session
        return token, session

    def end(self, session_id: str, reason: str = "signed out") -> bool:
        session = self._by_id.pop(session_id, None)
        if session is None:
            return False
        self._by_hash.pop(session.token_hash, None)
        for callback in list(self.on_end):
            try:
                callback(session)
            except Exception:  # noqa: BLE001 - ending a session must always complete
                pass
        return True

    def end_all(self, reason: str) -> int:
        ids = list(self._by_id)
        for sid in ids:
            self.end(sid, reason)
        return len(ids)

    def _expired(self, s: Session, now: float) -> bool:
        return now >= s.expires_at or (now - s.last_seen >= self.idle and not s.viewers)

    def sweep(self) -> int:
        now = time.time()
        return sum(self.end(s.id, "expired") for s in self.all() if self._expired(s, now))
