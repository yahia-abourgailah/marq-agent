import json

import pytest

from app.sql.agent import Refused, Sql
from app.sql.guard import SQLGuardError
from app.tools.sql import MAX_RESULT_CHARS, SQLTool, _fit_to_budget


class FakeSQLAgent:
    pass


class FakeRepository:
    async def execute_read(self, query, params=()):
        assert query == "SELECT COUNT(*) FROM deals;"
        return [{"count": 42}]


@pytest.mark.asyncio
async def test_sql_tool_empty_question():
    tool = SQLTool(
        sql_agent=FakeSQLAgent(),
        repository=FakeRepository(),
    )

    result = await tool.query("")

    assert result["success"] is False
    assert result["error"] == "The database question cannot be empty."


@pytest.mark.asyncio
async def test_sql_tool_returns_data(monkeypatch):
    async def fake_generate_sql(agent, question):
        assert question == "How many deals do we have?"
        return Sql(query="SELECT COUNT(*) FROM deals;")

    monkeypatch.setattr(
        "app.tools.sql.generate_sql",
        fake_generate_sql,
    )

    tool = SQLTool(
        sql_agent=FakeSQLAgent(),
        repository=FakeRepository(),
    )

    result = await tool.query("How many deals do we have?")

    assert result["success"] is True
    assert result["data"] == [{"count": 42}]
    assert result["row_count"] == 1


# ============================================================
# [claude] Failure taxonomy.
#
# Every failure used to collapse into {"success": False, "error": str(exc)},
# so a policy refusal, a guard rejection and a dead database were
# indistinguishable. The agent retried all three until it ran out of
# recursion budget. These pin the three outcomes apart.
# ============================================================


@pytest.mark.asyncio
async def test_refusal_is_reported_as_not_retryable(monkeypatch):
    """
    Asking for a masked column must come back as a clean refusal, not as a
    SQL syntax error. This is the case that used to end in
    GraphRecursionError.
    """

    async def fake_generate_sql(agent, question):
        return Refused(reason="That information is not available.")

    monkeypatch.setattr("app.tools.sql.generate_sql", fake_generate_sql)

    tool = SQLTool(sql_agent=FakeSQLAgent(), repository=FakeRepository())

    result = await tool.query("What is the total contract price?")

    assert result["success"] is False
    assert result["retryable"] is False
    assert result["reason"] == "not_available"
    assert result["error"] == "That information is not available."


@pytest.mark.asyncio
async def test_guard_rejection_is_retryable(monkeypatch):
    async def fake_generate_sql(agent, question):
        return Sql(query="SELECT * FROM pg_authid")

    class RejectingRepository:
        async def execute_read(self, query, params=()):
            raise SQLGuardError("Table 'pg_authid' is not allowed.")

    monkeypatch.setattr("app.tools.sql.generate_sql", fake_generate_sql)

    tool = SQLTool(sql_agent=FakeSQLAgent(), repository=RejectingRepository())

    result = await tool.query("show me the auth table")

    assert result["success"] is False
    assert result["retryable"] is True
    assert result["reason"] == "rejected_by_guard"


@pytest.mark.asyncio
async def test_infrastructure_error_is_not_retryable_and_leaks_nothing(
    monkeypatch,
):
    """
    Driver errors can carry DSN fragments and schema details. Only the
    exception type crosses the boundary.
    """

    async def fake_generate_sql(agent, question):
        return Sql(query="SELECT COUNT(*) FROM deals;")

    class BrokenRepository:
        async def execute_read(self, query, params=()):
            raise ConnectionError(
                "connection to host=10.0.0.1 user=secret_user failed"
            )

    monkeypatch.setattr("app.tools.sql.generate_sql", fake_generate_sql)

    tool = SQLTool(sql_agent=FakeSQLAgent(), repository=BrokenRepository())

    result = await tool.query("How many deals do we have?")

    assert result["success"] is False
    assert result["retryable"] is False
    assert result["reason"] == "error"
    assert result["error"] == "ConnectionError"
    assert "secret_user" not in str(result)
    assert "10.0.0.1" not in str(result)


# ============================================================
# [claude] Payload budget.
#
# MAX_ROWS caps row count but not row width. A 62-column deals row is ~570
# tokens, so 500 of them is ~285,000 — several times the model's whole
# context, from a single tool call.
# ============================================================


def test_narrow_results_pass_through_untouched():
    rows = [{"status": "contracted", "n": i} for i in range(500)]

    assert _fit_to_budget(rows) == rows


def test_wide_results_are_trimmed_to_the_budget():
    wide = [{f"col_{c}": "x" * 40 for c in range(62)} for _ in range(500)]

    kept = _fit_to_budget(wide)

    assert 0 < len(kept) < len(wide)
    assert len(json.dumps(kept)) <= MAX_RESULT_CHARS * 1.1


def test_one_oversized_row_is_still_returned():
    """Better a single visible row than an empty result the agent
    cannot explain."""

    monster = [{"blob": "x" * (MAX_RESULT_CHARS * 3)}]

    assert len(_fit_to_budget(monster)) == 1


@pytest.mark.asyncio
async def test_trimming_is_reported_to_the_agent(monkeypatch):
    """
    row_count must reflect what the agent can see and rows_available what
    the query matched, so it never reports a trimmed sample as a total.
    """

    wide = [{f"col_{c}": "x" * 60 for c in range(62)} for _ in range(400)]

    async def fake_generate_sql(agent, question):
        return Sql(query="SELECT * FROM deals")

    class WideRepository:
        async def execute_read(self, query, params=()):
            return wide

    monkeypatch.setattr("app.tools.sql.generate_sql", fake_generate_sql)

    tool = SQLTool(sql_agent=FakeSQLAgent(), repository=WideRepository())
    result = await tool.query("show me everything")

    assert result["success"] is True
    assert result["rows_available"] == 400
    assert result["row_count"] < 400
    assert result["truncated"] is True
    assert len(json.dumps(result["data"])) <= MAX_RESULT_CHARS * 1.1
