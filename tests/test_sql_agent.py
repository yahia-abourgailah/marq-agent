import pytest

from app.llm.model import get_model
from app.sql.agent import build_sql_agent, generate_sql


@pytest.mark.asyncio
async def test_sql_agent_generates_sql():
    model = get_model()

    agent = build_sql_agent(model)

    sql = await generate_sql(
        agent,
        "How many deals are there?",
    )

    print("\nGenerated SQL:")
    print(sql)

    assert sql
    assert "SELECT" in sql.upper()