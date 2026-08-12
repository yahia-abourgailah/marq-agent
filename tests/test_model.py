"""
[claude] Tests for chat model construction.

`temperature` was not passed to ChatOpenAI at all, so generation ran at the
endpoint's default — sampled rather than greedy. SQL generation is a
compilation step, and sampling it makes both the output and any eval over it
non-reproducible.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.llm.model import get_model


def test_model_is_deterministic_by_default():
    assert get_model().temperature == 0.0


def test_settings_default_temperature_is_zero():
    assert settings.model_temperature == 0.0


@pytest.mark.parametrize("temperature", [0.0, 0.3, 1.0])
def test_temperature_can_be_overridden_per_call_site(temperature):
    """
    The Deals Agent could take a higher temperature for prose while SQL
    generation stays at zero.
    """

    assert get_model(temperature=temperature).temperature == temperature


def test_timeout_and_retries_are_set():
    """Regression guard: an unbounded model call holds a vLLM slot open."""

    model = get_model()

    assert model.request_timeout == 30
    assert model.max_retries == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_generation_is_reproducible():
    """
    The behaviour temperature=0 buys us: the same question produces the same
    query, so a failing prompt test is a regression rather than an unlucky
    sample.

    [claude] Compared as normalised SQL rather than raw strings. temperature=0
    makes decoding greedy, but the vLLM endpoint is shared and batched
    inference is not bitwise deterministic — results can vary slightly with
    batch composition. Normalising through sqlglot absorbs formatting jitter
    while still catching a genuinely different query.
    """

    import sqlglot

    from app.sql.agent import Sql, build_sql_agent, generate_sql

    agent = build_sql_agent(get_model())
    question = "How many contracted deals do we have?"

    normalised = set()
    for _ in range(3):
        result = await generate_sql(agent=agent, question=question)
        assert isinstance(result, Sql), f"expected SQL, got {result!r}"
        normalised.add(
            sqlglot.parse_one(result.query, read="postgres").sql(dialect="postgres")
        )

    assert len(normalised) == 1, (
        "SQL generation is not reproducible across runs:\n"
        + "\n".join(f"  {q}" for q in sorted(normalised))
    )
