import pytest

from app.db.connection import app_db
from app.db.repositories.deals import DealsRepository
from app.llm.model import get_model
from app.sql.agent import build_sql_agent, generate_sql
from app.sql.executor import SQLExecutor
from app.sql.guard import SQLGuard
from app.tools.sql import SQLTool


@pytest.mark.asyncio
async def test_real_sql_pipeline():
    model = get_model()

    sql_agent = build_sql_agent(model)

    question = "How many active deals do we have?"

    generated_sql = await generate_sql(
        agent=sql_agent,
        question=question,
    )

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