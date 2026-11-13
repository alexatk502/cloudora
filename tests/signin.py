"""What the sign-in page's script does in the browser, for tests: turn the key into a one-time proof."""

from __future__ import annotations

import hashlib
import re

from cloudora import secret


def form(page: str, key: str, origin: str) -> dict[str, str]:
    """The fields the browser posts to /login after typing ``key`` on the sign-in ``page`` served at ``origin``."""
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    challenge = re.search(r'name="challenge" value="([^"]+)"', page).group(1)
    digest = hashlib.sha256(secret.normalise(key).encode()).digest()
    return {"csrf": csrf, "challenge": challenge, "proof": secret.proof(digest, challenge, origin)}
