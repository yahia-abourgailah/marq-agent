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

from app.api.ratelimit import WINDOW_SECONDS, RateLimiter
from tests.api_support import StubGraph, TokenIssuer, build_app, client


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


# ============================================================
# Per-caller request limit
# ============================================================


def test_a_caller_is_cut_off_after_the_limit():
    limiter = RateLimiter(per_minute=3)

    assert [limiter.check("alice")[0] for _ in range(3)] == [True] * 3
    assert limiter.check("alice")[0] is False


def test_one_caller_hitting_the_limit_does_not_affect_another():
    """The key is the subject, so a runaway tab throttles its own user."""

    limiter = RateLimiter(per_minute=2)

    for _ in range(5):
        limiter.check("alice")

    assert limiter.check("bob")[0] is True


def test_hammering_after_a_rejection_does_not_reset_the_window():
    """
    [claude] A client that ignores its 429 and keeps retrying is the exact
    client this exists for. If continued requests restarted the window, the
    limit would be loosest against the caller abusing it hardest.
    """

    limiter = RateLimiter(per_minute=2)

    for _ in range(20):
        limiter.check("alice")

    allowed, retry_after = limiter.check("alice")

    assert allowed is False
    assert 0 < retry_after <= WINDOW_SECONDS


def test_the_window_expires():
    limiter = RateLimiter(per_minute=1)

    assert limiter.check("alice")[0] is True
    assert limiter.check("alice")[0] is False

    # Reach into the window's start rather than sleeping a minute.
    limiter._counts["alice"][0] -= WINDOW_SECONDS + 1

    assert limiter.check("alice")[0] is True


def test_expired_windows_are_pruned():
    """
    Otherwise the dictionary is a slow leak keyed on every subject that
    ever called — unbounded, for a service with staff turnover, in exactly
    the way that never shows up in testing.
    """

    limiter = RateLimiter(per_minute=5)

    for i in range(50):
        limiter.check(f"user-{i}")

    for window in limiter._counts.values():
        window[0] -= WINDOW_SECONDS * 3

    assert limiter.prune() == 50
    assert limiter._counts == {}


def test_a_disabled_limit_allows_everything():
    """Zero means off, for a deployment that limits at the edge instead."""

    limiter = RateLimiter(per_minute=0)

    assert all(limiter.check("alice")[0] for _ in range(1000))


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
