"""
[claude] Hermetic tests for how generate_sql classifies model output.

The refusal path is load-bearing: a masked-column question used to produce
prose, fail SQLGuard parsing, surface as "Invalid SQL query.", and send the
Deals Agent into a retry loop that ended in GraphRecursionError. These pin the
classification without needing a live model.
"""

from __future__ import annotations

import pytest

from app.sql.agent import REFUSAL_PREFIX, Refused, Sql, generate_sql


class FakeAgent:
    """Returns one fixed assistant message, like the SQL agent would."""

    def __init__(self, content):
        self.content = content

    async def ainvoke(self, _payload):
        class Message:
            def __init__(self, content):
                self.content = content

        return {"messages": [Message(self.content)]}


async def run(content):
    return await generate_sql(agent=FakeAgent(content), question="q")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "SELECT COUNT(*) FROM deals",
        "  select id from deals  ",
        "WITH x AS (SELECT 1 AS n) SELECT n FROM x",
        "```sql\nSELECT COUNT(*) FROM deals\n```",
    ],
)
async def test_queries_are_classified_as_sql(content):
    result = await run(content)

    assert isinstance(result, Sql)
    assert result.query


@pytest.mark.asyncio
async def test_sentinel_is_classified_as_refusal():
    result = await run(
        f"{REFUSAL_PREFIX} that information is not available through this agent."
    )

    assert isinstance(result, Refused)
    assert result.reason == (
        "that information is not available through this agent."
    )


@pytest.mark.asyncio
async def test_sentinel_without_a_reason_still_refuses():
    result = await run(REFUSAL_PREFIX)

    assert isinstance(result, Refused)
    assert result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        # The exact output that used to reach SQLGuard and be reported as a
        # syntax error.
        "the requested information cannot be retrieved through the available "
        "CRM data access.",
        "I'm sorry, I cannot help with that.",
        "The deals table has no contract_price column available to me.",
    ],
)
async def test_prose_without_the_sentinel_is_still_treated_as_refusal(content):
    """
    Safety net: if the model ignores the contract and answers in prose, that
    must not be mistaken for SQL.
    """

    result = await run(content)

    assert isinstance(result, Refused)


@pytest.mark.asyncio
async def test_empty_output_raises():
    with pytest.raises(ValueError):
        await run("   ")
