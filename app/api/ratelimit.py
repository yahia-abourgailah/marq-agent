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
that has to be kept somewhere, and this deliberately keeps as little as it
can.

Two backends, and which one is in force is a deployment fact
-----------------------------------------------------------
`RateLimiter` keeps its counters in a dictionary, so the effective limit is
the configured one times the worker count. `RedisRateLimiter` keeps them in
Redis, so every worker shares one window and the configured number is the
number.

`build_rate_limiter()` picks between them on `REDIS_URL`, and the choice is
reported in the boot log rather than inferred — the two behave identically
until the second worker exists, which is precisely when nobody is looking.

`check` is async on both. The in-memory one has nothing to await and says
so, but the call site is one `await` either way, which is what keeps the
selection a configuration detail instead of a branch in `deps.py`.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

logger = logging.getLogger("marq.api")

WINDOW_SECONDS = 60

# [claude] Short, and on the request path on purpose.
#
# A rate limiter that blocks for the default socket timeout has become the
# outage it exists to survive. Quarter of a second is far more than a
# healthy round trip to Redis and far less than a user notices, and what
# lies past it is the in-memory fallback rather than an error.
REDIS_TIMEOUT_SECONDS = 0.25

# [claude] How long a failure is believed before Redis is tried again.
#
# Measured rather than assumed, and it exists because the first version of
# this file did not have it. Against a blackholed address — packets dropped
# rather than refused, which is what a hung Redis looks like — every single
# request paid the full connect timeout: 0.253s on the first call and
# 0.251s on the second, for as long as the outage lasted. "Degraded" has to
# mean *stop asking*, or an outage in the cache becomes a latency incident
# in the API.
#
# Five seconds is one 250ms probe per five seconds of outage, and a
# recovery noticed within five seconds of it happening.
REDIS_RETRY_SECONDS = 5.0


class RateLimiter:
    """Fixed-window request counter, keyed on whatever the caller passes."""

    backend = "memory"

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._counts: dict[str, list[float | int]] = defaultdict(
            lambda: [0.0, 0]
        )

    async def check(self, key: str) -> tuple[bool, int]:
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

    async def aclose(self) -> None:
        """Nothing to release. Present so the call site needs no branch."""

        return None


class RedisRateLimiter:
    """
    The same fixed window, counted in Redis so every worker shares it.

    [claude] The window is aligned to the wall clock rather than to the
    caller's first request, and that difference is forced rather than
    chosen. Two workers can agree on `int(time.time()) // 60` without
    talking to each other; they cannot agree on "when did alice first
    call" without a round trip to find out, and a limiter that reads
    before it writes is a limiter with a race in it.

    What that changes for a caller: the window boundary is the same for
    everyone, so `Retry-After` counts down to the top of the minute rather
    than to sixty seconds after their own first request. The guarantee the
    limit exists for — a runaway tab is stopped within a minute — is
    unchanged.

    Everything else is preserved deliberately. `INCR` runs on the rejected
    request too, so hammering does not buy a reset. The key carries its own
    window number, so an expiring key is never a key still in use.
    """

    backend = "redis"

    def __init__(
        self,
        per_minute: int,
        client,
        *,
        prefix: str = "marq:ratelimit:",
    ) -> None:
        self.per_minute = per_minute
        self._client = client
        self._prefix = prefix

        # [claude] Not a spare copy — the thing that runs when Redis is
        # unreachable. Degrading to a per-process limit is the behaviour
        # this service had until today; degrading to no limit at all would
        # hand a retry loop the endpoint on the day the cache blinks.
        self._fallback = RateLimiter(per_minute)
        self._degraded = False

        # The deadline before which Redis is not worth asking. See
        # REDIS_RETRY_SECONDS.
        self._degraded_until = 0.0

    async def check(self, key: str) -> tuple[bool, int]:
        if self.per_minute <= 0:
            # No round trip for a limit that is switched off.
            return True, 0

        now = time.time()

        # Circuit open. Falling back without a round trip is the difference
        # between an outage that is invisible and one that adds the connect
        # timeout to every request in the service.
        if now < self._degraded_until:
            return await self._fallback.check(key)

        window = int(now // WINDOW_SECONDS)
        redis_key = f"{self._prefix}{key}:{window}"
        retry_after = max(1, int(WINDOW_SECONDS - (now % WINDOW_SECONDS)))

        try:
            pipe = self._client.pipeline()
            pipe.incr(redis_key)
            # Twice the window, so a clock that drifts between workers
            # cannot expire a key one of them is still counting in.
            pipe.expire(redis_key, WINDOW_SECONDS * 2)
            count = int((await pipe.execute())[0])
        except Exception as exc:  # noqa: BLE001 - see below
            # [claude] A wide net on purpose. Every failure mode here —
            # connection refused, timeout, a MISCONF from a Redis that
            # cannot snapshot, a client library raising something this
            # module has never heard of — has the same correct answer:
            # fall back and keep serving. A rate limiter must not be the
            # component that takes the API down.
            self._degrade(exc, now)
            return await self._fallback.check(key)

        self._recover()

        if count == 1:
            return True, 0

        if count > self.per_minute:
            return False, retry_after

        return True, retry_after

    def prune(self, now: float | None = None) -> int:
        """
        Redis expires its own keys; this prunes whatever the fallback kept.

        Returning the fallback's count rather than zero keeps the number
        honest — after an outage there really is a dictionary to clear.
        """

        return self._fallback.prune(now)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _degrade(self, exc: Exception, now: float) -> None:
        """Hold the circuit open, and log the transition — not the request."""

        self._degraded_until = now + REDIS_RETRY_SECONDS

        # A probe that fails is the same outage, not a new one. One line
        # per outage; the recovery gets the matching one.
        if self._degraded:
            return

        self._degraded = True
        logger.warning(
            "rate_limiter_degraded",
            extra={
                "backend": "memory",
                "reason": f"{type(exc).__name__}: {exc}",
                "effect": "limit is now per process, not per deployment",
            },
        )

    def _recover(self) -> None:
        if not self._degraded:
            return

        self._degraded = False
        self._degraded_until = 0.0
        logger.info("rate_limiter_recovered", extra={"backend": "redis"})


def build_rate_limiter(
    per_minute: int, redis_url: str | None
) -> RateLimiter | RedisRateLimiter:
    """
    Pick a backend. Empty `REDIS_URL` means the dictionary.

    [claude] Unset is a real deployment, not an oversight: a single-worker
    run and the whole test suite both want the in-memory one, and
    `.env.development.example` ships it empty. So this returns the weaker
    limiter without complaint, and the boot log says which one it built.

    Redis is imported here rather than at module scope so that a
    deployment which does not use it does not pay for the import — the
    same reason `app/tools/web.py` defers Tavily.
    """

    if not redis_url:
        return RateLimiter(per_minute)

    from redis.asyncio import Redis

    client = Redis.from_url(
        redis_url,
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
        # A retry inside a limiter whose fallback is already correct only
        # spends the caller's latency to reach the same place.
        retry_on_timeout=False,
    )

    return RedisRateLimiter(per_minute, client)


__all__ = [
    "REDIS_RETRY_SECONDS",
    "RateLimiter",
    "RedisRateLimiter",
    "WINDOW_SECONDS",
    "build_rate_limiter",
]
