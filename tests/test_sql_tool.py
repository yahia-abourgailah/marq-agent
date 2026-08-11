import pytest

from app.tools.sql import SQLTool


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
        return "SELECT COUNT(*) FROM deals;"

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