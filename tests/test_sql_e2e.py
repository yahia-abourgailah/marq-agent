import pytest

from app.db.connection import app_db
from app.db.repositories.deals import DealsRepository
from app.llm.model import get_model
from app.sql.agent import Sql, build_sql_agent, generate_sql
from app.sql.executor import SQLExecutor
from app.sql.guard import SQLGuard
from app.tools.sql import SQLTool

# [claude] Marked as integration: this module runs the whole pipeline
# against a live model endpoint and PostgreSQL.
# Excluded from the default run (see pyproject.toml); use
#     pytest -m integration
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_sql_pipeline():
    model = get_model()

    sql_agent = build_sql_agent(model)

    question = "How many active deals do we have?"

    generated_sql = await generate_sql(
        agent=sql_agent,
        question=question,
    )
    assert isinstance(generated_sql, Sql), f"expected SQL, got {generated_sql!r}"
    generated_sql = generated_sql.query

    print("\nGenerated SQL:")
    print(generated_sql)

    executor = SQLExecutor(
        database=app_db,
    )

    guard = SQLGuard()

    repository = DealsRepository(
        executor=executor,
        guard=guard,
    )

    sql_tool = SQLTool(
        sql_agent=sql_agent,
        repository=repository,
    )

    await app_db.connect()

    try:
        result = await sql_tool.query(question)

        print("\nResult:")
        print(result)

        assert result["success"] is True
        assert result["row_count"] > 0

    finally:
        await app_db.close()
