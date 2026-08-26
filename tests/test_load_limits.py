"""
[claude] Ceilings on what one turn, and one caller, may consume.

From the 24 August review (A4): a turn is up to five model calls — routing,
two specialists in parallel, a nested SQL-agent call inside each, then
synthesis — and nothing bounded them. Against a vLLM server running
`--max-num-seqs 32`, a handful of simultaneous users saturates the endpoint
and everyone queues. There was also no per-user request limit, so "a browser
tab in a retry loop is unbounded spend".

Two different ceilings, and they protect different things: the semaphore
bounds what this process does to a shared endpoint, the rate limit bounds
what one caller does to this process.
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.ratelimit import (
    REDIS_RETRY_SECONDS,
    WINDOW_SECONDS,
    RateLimiter,
    RedisRateLimiter,
    build_rate_limiter,
)
from tests.api_support import StubGraph, TokenIssuer, build_app, client


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


# ============================================================
# Per-caller request limit
# ============================================================


@pytest.mark.asyncio
async def test_a_caller_is_cut_off_after_the_limit():
    limiter = RateLimiter(per_minute=3)

    allowed = [(await limiter.check("alice"))[0] for _ in range(3)]

    assert allowed == [True] * 3
    assert (await limiter.check("alice"))[0] is False


@pytest.mark.asyncio
async def test_one_caller_hitting_the_limit_does_not_affect_another():
    """The key is the subject, so a runaway tab throttles its own user."""

    limiter = RateLimiter(per_minute=2)

    for _ in range(5):
        await limiter.check("alice")

    assert (await limiter.check("bob"))[0] is True


@pytest.mark.asyncio
async def test_hammering_after_a_rejection_does_not_reset_the_window():
    """
    [claude] A client that ignores its 429 and keeps retrying is the exact
    client this exists for. If continued requests restarted the window, the
    limit would be loosest against the caller abusing it hardest.
    """

    limiter = RateLimiter(per_minute=2)

    for _ in range(20):
        await limiter.check("alice")

    allowed, retry_after = await limiter.check("alice")

    assert allowed is False
    assert 0 < retry_after <= WINDOW_SECONDS


@pytest.mark.asyncio
async def test_the_window_expires():
    limiter = RateLimiter(per_minute=1)

    assert (await limiter.check("alice"))[0] is True
    assert (await limiter.check("alice"))[0] is False

    # Reach into the window's start rather than sleeping a minute.
    limiter._counts["alice"][0] -= WINDOW_SECONDS + 1

    assert (await limiter.check("alice"))[0] is True


@pytest.mark.asyncio
async def test_expired_windows_are_pruned():
    """
    Otherwise the dictionary is a slow leak keyed on every subject that
    ever called — unbounded, for a service with staff turnover, in exactly
    the way that never shows up in testing.
    """

    limiter = RateLimiter(per_minute=5)

    for i in range(50):
        await limiter.check(f"user-{i}")

    for window in limiter._counts.values():
        window[0] -= WINDOW_SECONDS * 3

    assert limiter.prune() == 50
    assert limiter._counts == {}


@pytest.mark.asyncio
async def test_a_disabled_limit_allows_everything():
    """Zero means off, for a deployment that limits at the edge instead."""

    limiter = RateLimiter(per_minute=0)

    for _ in range(1000):
        assert (await limiter.check("alice"))[0] is True


@pytest.mark.asyncio
async def test_the_api_returns_429_with_a_retry_after(issuer, monkeypatch):
    """Through HTTP, because the header is half of what makes it usable."""

    from app.api import deps

    monkeypatch.setattr(
        deps.settings, "rate_limit_per_minute", 2, raising=False
    )
    deps.reset_rate_limiter()

    app, _ = build_app(verifier=issuer.verifier(), graph=StubGraph())

    async with client(app) as http:
        codes = []
        for _ in range(4):
            response = await http.post(
                "/v1/chat", json={"message": "hi"}, headers=issuer.auth()
            )
            codes.append(response.status_code)

    assert codes[:2] == [200, 200]
    assert codes[-1] == 429
    assert response.headers.get("Retry-After")
    assert "Too many requests" in response.text

    deps.reset_rate_limiter()


# ============================================================
# The shared window — one limit across every worker
# ============================================================
#
# [claude] The point of the Redis backend, and the reason it is worth
# testing against a double rather than a server: the in-memory limiter is
# indistinguishable from this one inside a single process. It only tells
# the truth about itself when two of them exist, which is what these
# construct — two limiters, one store, exactly as two uvicorn workers
# would be.


class FakePipeline:
    """Just the two commands the counter issues."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple] = []

    def incr(self, key: str):
        self._ops.append(("incr", key))
        return self

    def expire(self, key: str, seconds: int):
        self._ops.append(("expire", key, seconds))
        return self

    async def execute(self) -> list:
        self._redis.executions += 1

        if self._redis.failing:
            raise ConnectionError("Error 61 connecting to redis:6379")

        results = []

        for op in self._ops:
            if op[0] == "incr":
                self._redis.store[op[1]] = self._redis.store.get(op[1], 0) + 1
                results.append(self._redis.store[op[1]])
            else:
                self._redis.ttls[op[1]] = op[2]
                results.append(True)

        return results


class FakeRedis:
    """
    Enough of `redis.asyncio.Redis` for INCR, EXPIRE and a pipeline.

    `failing` flips the whole client, because that is how the real failure
    arrives — not one bad command, but a server that has gone away.
    """

    def __init__(self, failing: bool = False) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.failing = failing
        self.closed = False
        self.executions = 0

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_two_workers_share_one_window():
    """
    The whole reason this backend exists.

    With the in-memory limiter these two objects are two independent
    ceilings, so a limit of three permits six. Against one store the
    fourth request is the fourth request, whichever worker takes it.
    """

    redis = FakeRedis()
    worker_a = RedisRateLimiter(per_minute=3, client=redis)
    worker_b = RedisRateLimiter(per_minute=3, client=redis)

    assert (await worker_a.check("alice"))[0] is True
    assert (await worker_b.check("alice"))[0] is True
    assert (await worker_a.check("alice"))[0] is True

    assert (await worker_b.check("alice"))[0] is False


@pytest.mark.asyncio
async def test_one_caller_hitting_the_shared_limit_does_not_affect_another():
    redis = FakeRedis()
    limiter = RedisRateLimiter(per_minute=2, client=redis)

    for _ in range(5):
        await limiter.check("alice")

    assert (await limiter.check("bob"))[0] is True


@pytest.mark.asyncio
async def test_a_rejected_request_still_counts_in_redis():
    """
    Same rule as the dictionary: the window resets on time, not on good
    behaviour. INCR runs before the verdict, so it cannot be otherwise —
    this holds that it stays that way.
    """

    redis = FakeRedis()
    limiter = RedisRateLimiter(per_minute=1, client=redis)

    await limiter.check("alice")

    for _ in range(9):
        assert (await limiter.check("alice"))[0] is False

    assert list(redis.store.values()) == [10]


@pytest.mark.asyncio
async def test_every_counter_carries_a_ttl():
    """
    Otherwise the leak the dictionary has is simply moved into Redis,
    where nothing prunes it and nobody looks.
    """

    redis = FakeRedis()
    limiter = RedisRateLimiter(per_minute=5, client=redis)

    await limiter.check("alice")

    assert set(redis.ttls) == set(redis.store)
    assert all(ttl >= WINDOW_SECONDS for ttl in redis.ttls.values())


@pytest.mark.asyncio
async def test_the_key_is_scoped_to_its_window():
    """
    How the window rolls without anyone resetting it: last minute's count
    lives under last minute's key, so the new window opens empty.

    Asserted by seeding the neighbouring windows rather than by moving the
    clock — a test that patches `time.time` would be testing the patch.
    """

    redis = FakeRedis()
    limiter = RedisRateLimiter(per_minute=1, client=redis)

    await limiter.check("alice")
    (current_key,) = list(redis.store)
    prefix, _, window = current_key.rpartition(":")

    # A full window's worth of history on either side.
    redis.store[f"{prefix}:{int(window) - 1}"] = 99
    redis.store[f"{prefix}:{int(window) + 1}"] = 99

    assert redis.store[current_key] == 1
    assert (await limiter.check("alice"))[0] is False


@pytest.mark.asyncio
async def test_redis_falling_over_degrades_to_a_process_limit():
    """
    [claude] The failure this is allowed to have.

    A cache outage must not become an API outage, and it must not become
    an unlimited API either. What is left is the per-process ceiling this
    service ran on until the backend existed — weaker, still a limit, and
    logged as a transition rather than per request.
    """

    limiter = RedisRateLimiter(per_minute=2, client=FakeRedis(failing=True))

    assert (await limiter.check("alice"))[0] is True
    assert (await limiter.check("alice"))[0] is True
    assert (await limiter.check("alice"))[0] is False

    # Still keyed per caller while degraded.
    assert (await limiter.check("bob"))[0] is True


@pytest.mark.asyncio
async def test_a_degraded_limiter_stops_asking_redis():
    """
    [claude] The measurement that produced `REDIS_RETRY_SECONDS`.

    Against a blackholed address the first version of this class paid the
    full connect timeout on *every* request — 0.253s, then 0.251s, for the
    length of the outage. Falling back is only half of surviving one; the
    other half is not asking again for a while.
    """

    redis = FakeRedis(failing=True)
    limiter = RedisRateLimiter(per_minute=5, client=redis)

    await limiter.check("alice")
    assert redis.executions == 1

    for _ in range(50):
        await limiter.check("alice")

    assert redis.executions == 1, "the circuit should still be open"


@pytest.mark.asyncio
async def test_redis_coming_back_is_used_again():
    redis = FakeRedis(failing=True)
    limiter = RedisRateLimiter(per_minute=10, client=redis)

    await limiter.check("alice")
    assert redis.store == {}

    # Let the cooldown lapse rather than sleeping through it. The deadline
    # is the whole mechanism, so moving it is moving the clock.
    limiter._degraded_until -= REDIS_RETRY_SECONDS + 1
    redis.failing = False

    await limiter.check("alice")

    assert list(redis.store.values()) == [1]
    assert limiter._degraded is False


@pytest.mark.asyncio
async def test_a_failed_probe_reopens_the_circuit():
    """An outage that is still going is one outage, not a new one each probe."""

    redis = FakeRedis(failing=True)
    limiter = RedisRateLimiter(per_minute=5, client=redis)

    await limiter.check("alice")
    limiter._degraded_until -= REDIS_RETRY_SECONDS + 1

    await limiter.check("alice")

    assert redis.executions == 2, "the probe should have been attempted"
    assert limiter._degraded_until > 0, "and the circuit held open again"

    for _ in range(20):
        await limiter.check("alice")

    assert redis.executions == 2


@pytest.mark.asyncio
async def test_pruning_clears_what_the_outage_left_behind():
    """Redis expires its own keys; the fallback's dictionary does not."""

    limiter = RedisRateLimiter(per_minute=5, client=FakeRedis(failing=True))

    for i in range(20):
        await limiter.check(f"user-{i}")

    for window in limiter._fallback._counts.values():
        window[0] -= WINDOW_SECONDS * 3

    assert limiter.prune() == 20


@pytest.mark.asyncio
async def test_a_disabled_limit_never_reaches_redis():
    """Zero means off, and off should not cost a round trip per request."""

    redis = FakeRedis()
    limiter = RedisRateLimiter(per_minute=0, client=redis)

    for _ in range(100):
        assert (await limiter.check("alice"))[0] is True

    assert redis.store == {}


@pytest.mark.asyncio
async def test_the_backend_is_chosen_by_configuration():
    """
    Unset is a deployment, not an oversight — a single worker and the
    whole test suite both want the dictionary.
    """

    assert isinstance(build_rate_limiter(30, ""), RateLimiter)
    assert isinstance(build_rate_limiter(30, None), RateLimiter)

    configured = build_rate_limiter(30, "redis://localhost:6379/0")

    assert isinstance(configured, RedisRateLimiter)
    assert configured.backend == "redis"

    # Constructed, never connected: `from_url` is lazy, which is what lets
    # this run with no server anywhere near it.
    await configured.aclose()


@pytest.mark.asyncio
async def test_closing_releases_the_client():
    redis = FakeRedis()

    await RedisRateLimiter(per_minute=5, client=redis).aclose()

    assert redis.closed is True


# ============================================================
# Concurrent model calls
# ============================================================


@pytest.mark.asyncio
async def test_model_calls_are_capped_across_the_process():
    """
    [claude] Asserted on observed concurrency rather than on the semaphore's
    value, because the value is not the property that matters — a ceiling
    that is never reached and a ceiling that is not applied look identical
    from the outside.
    """

    from app.llm import model as model_module

    model_module._slots = None
    model_module._slots_loop = None

    from app.config import settings

    original = settings.max_concurrent_model_calls
    settings.max_concurrent_model_calls = 3

    live = 0
    peak = 0

    async def call():
        nonlocal live, peak
        async with model_module.model_slot():
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

    try:
        await asyncio.gather(*(call() for _ in range(20)))
    finally:
        settings.max_concurrent_model_calls = original
        model_module._slots = None
        model_module._slots_loop = None

    assert peak <= 3, f"{peak} calls were in flight against a ceiling of 3"
    assert peak > 1, "the calls did not actually overlap, so nothing was tested"


# ============================================================
# Thread length
# ============================================================


@pytest.mark.asyncio
async def test_an_overlong_thread_is_refused_in_words(issuer, monkeypatch):
    """
    [claude] The review's S1, and the point is the wording.

    The failure this replaces was a provider error on an overflowing
    context, caught as a generic specialist failure, on a checkpointed
    state — so every later turn on that thread failed identically and
    nothing in the reply told the user to start a new conversation. The
    thread could not be recovered and there was no way to learn that.

    Trimming keeps a long thread working. It cannot tell the user that the
    beginning of their conversation has stopped being read, which is what
    this does.
    """

    from app.api import deps
    from tests.api_support import FakeConversations

    monkeypatch.setattr(
        deps.settings, "max_turns_per_thread", 3, raising=False
    )

    import app.api.routes.chat as chat_route

    monkeypatch.setattr(
        chat_route.settings, "max_turns_per_thread", 3, raising=False
    )

    conversations = FakeConversations()
    app, _ = build_app(
        verifier=issuer.verifier(),
        graph=StubGraph(),
        conversations=conversations,
    )

    async with client(app) as http:
        first = await http.post(
            "/v1/chat",
            json={"message": "hi", "thread_id": "t1"},
            headers=issuer.auth(),
        )
        assert first.status_code == 200

        # Fast-forward the recorded count past the cap.
        row = conversations.rows[("employee-1", "t1")]
        conversations.rows[("employee-1", "t1")] = type(row)(
            thread_id=row.thread_id,
            title=row.title,
            created_at=row.created_at,
            updated_at=row.updated_at,
            turn_count=3,
        )

        blocked = await http.post(
            "/v1/chat",
            json={"message": "and again", "thread_id": "t1"},
            headers=issuer.auth(),
        )

        fresh = await http.post(
            "/v1/chat", json={"message": "starting over"}, headers=issuer.auth()
        )

    assert blocked.status_code == 409
    assert "start a new conversation" in blocked.text.lower()
    assert "3 turns" in blocked.text

    # The remedy the message names has to actually work.
    assert fresh.status_code == 200


@pytest.mark.asyncio
async def test_the_streaming_path_refuses_an_overlong_thread_too(
    issuer, monkeypatch
):
    """
    [claude] Both endpoints, because a limit one path enforces is not a
    limit — and the streaming path is the one the UI uses.
    """

    import app.api.routes.chat as chat_route
    from tests.api_support import FakeConversations

    monkeypatch.setattr(
        chat_route.settings, "max_turns_per_thread", 1, raising=False
    )

    conversations = FakeConversations()
    app, _ = build_app(
        verifier=issuer.verifier(),
        graph=StubGraph(),
        conversations=conversations,
    )

    async with client(app) as http:
        await http.post(
            "/v1/chat/stream",
            json={"message": "hi", "thread_id": "t1"},
            headers=issuer.auth(),
        )
        blocked = await http.post(
            "/v1/chat/stream",
            json={"message": "again", "thread_id": "t1"},
            headers=issuer.auth(),
        )

    assert blocked.status_code == 409
