"""In-memory per-IP rate limiter.

Stdlib only — a per-IP interval check is all the LAN use case needs; not
worth a dependency.
"""
from __future__ import annotations

import os
import time

DEFAULT_RATE_LIMIT_S = float(os.environ.get("RATE_LIMIT_SECONDS", "10"))


class RateLimiter:
    def __init__(self, interval_s: float = DEFAULT_RATE_LIMIT_S):
        self._interval = interval_s
        self._last: dict[str, float] = {}

    def check(self, key: str, *, now: float | None = None) -> float:
        """Return seconds until next allowed request (0.0 if allowed now).

        On allowed, the timestamp is recorded. On denied, the timestamp is
        NOT bumped — denied requests don't extend the cool-down.
        """
        t = now if now is not None else time.monotonic()
        last = self._last.get(key)
        if last is None or (t - last) >= self._interval:
            self._last[key] = t
            return 0.0
        return self._interval - (t - last)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._last.clear()
        else:
            self._last.pop(key, None)
