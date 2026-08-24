"""
[claude] A per-caller request ceiling.

From the 24 August review, which paired it with the model-call semaphore:
one bounds what this process does to the endpoint, this bounds what one
caller does to this process. Without it "a browser tab in a retry loop is
unbounded spend" — and a chat UI that reconnects on error is exactly the
shape that produces one.

Keyed on the verified subject rather than on an IP. The whole API is
authenticated, every user sits behind the same office NAT, and an IP-keyed
limit would therefore throttle the company rather than the tab.

A fixed window rather than a sliding one or a token bucket. It is
approximate at the boundary — a caller can spend two windows' worth across
one instant — and that is fine for a limit whose job is to stop a runaway
loop rather than to meter billing. The alternatives cost per-request state
that has to be kept somewhere, and this deliberately keeps nothing.

In memory, and therefore per process. With several uvicorn workers the
effective limit is the configured one times the worker count. That is
stated rather than hidden: it is the right trade while conversations are
the thing that must be shared and rate limits are not, and the honest fix
when it stops being right is Redis, not a cleverer dictionary.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

logger = logging.getLogger("marq.api")

WINDOW_SECONDS = 60


class RateLimiter:
    """Fixed-window request counter, keyed on whatever the caller passes."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._counts: dict[str, list[float | int]] = defaultdict(
            lambda: [0.0, 0]
        )

    def check(self, key: str) -> tuple[bool, int]:
        """
        Record a request. Returns (allowed, seconds until the window resets).

        Counting the rejected request too, deliberately: a client that keeps
        hammering after a 429 should not have its window reset by the
        hammering. It resets on time, not on good behaviour.
        """

        if self.per_minute <= 0:
            return True, 0

        now = time.monotonic()
        window = self._counts[key]
        started, count = window[0], int(window[1])

        if now - started >= WINDOW_SECONDS:
            window[0], window[1] = now, 1
            return True, 0

        window[1] = count + 1
        retry_after = max(1, int(WINDOW_SECONDS - (now - started)))

        if count + 1 > self.per_minute:
            return False, retry_after

        return True, retry_after

    def prune(self, now: float | None = None) -> int:
        """
        Drop windows that have expired.

        [claude] Without this the dictionary is a slow memory leak keyed on
        every subject that ever called — which for a service with staff
        turnover is unbounded in exactly the way that never shows up in
        testing.
        """

        now = time.monotonic() if now is None else now
        stale = [
            key
            for key, window in self._counts.items()
            if now - window[0] >= WINDOW_SECONDS * 2
        ]

        for key in stale:
            del self._counts[key]

        return len(stale)


__all__ = ["RateLimiter", "WINDOW_SECONDS"]
