import pytest

from app.llm.model import get_model
from app.sql.agent import Sql, build_sql_agent, generate_sql

# [claude] Marked as integration: this module calls the live model endpoint.
# Excluded from the default run (see pyproject.toml); use
#     pytest -m integration
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_sql_agent_generates_sql():
    model = get_model()

    agent = build_sql_agent(model)

    sql = await generate_sql(
        agent,
        "How many deals are there?",
    )
    assert isinstance(sql, Sql), f"expected SQL, got {sql!r}"
    sql = sql.query

    print("\nGenerated SQL:")
    print(sql)

    assert sql
    assert "SELECT" in sql.upper()
