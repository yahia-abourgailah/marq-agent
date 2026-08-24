"""
[claude] Suite-wide fixtures.

Created 24 August 2026, when per-subject rate limiting was added. There was
no conftest before this and there is deliberately little in it now — the
existing suite keeps its builders in named modules (`api_support.py`,
`workspace_support.py`) so a reader can see where a fixture comes from, and
this file is only for things that must apply to every test whether it asks
or not.
"""

from __future__ import annotations

import pytest

from app.api.deps import reset_rate_limiter


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    """
    Give every test its own rate-limit window.

    The limiter is process-wide, which is correct in production and wrong
    for a suite that makes hundreds of requests as `employee-1`: without
    this the first thirty API tests pass and the rest fail with 429s that
    have nothing to do with what they assert. Seventeen did exactly that
    when the limit was first wired in.

    Autouse rather than opt-in, because the tests that would fail are the
    ones that never thought about rate limiting — which is most of them.
    """

    reset_rate_limiter()
    yield
    reset_rate_limiter()
