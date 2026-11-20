"""Password hashing, one-time tokens and a small login rate limiter (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verifying against this when the email is unknown keeps response times similar.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    """At most `limit` failures per key inside `window` seconds (in memory, per process)."""

    def __init__(self, limit: int = 10, window: int = 15 * 60):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key):
        hits, cutoff = self._hits[key], time.time() - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits

    def blocked(self, *keys) -> bool:
        with self._lock:
            return any(len(self._trim(k)) >= self.limit for k in keys)

    def count(self, key) -> int:
        with self._lock:
            return len(self._trim(key))

    def hit(self, *keys):
        with self._lock:
            for k in keys:
                self._trim(k).append(time.time())

    def reset(self, *keys):
        with self._lock:
            for k in keys:
                self._hits.pop(k, None)
