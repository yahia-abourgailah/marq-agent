import pytest

from app.llm.model import get_model
from app.sql.agent import build_sql_agent, generate_sql


QUESTIONS = [
    "How many active deals do we have?",
    "How many deals are in negotiation?",
    "What is the total value of active deals?",
    "How many deals does Sara Mostafa own?",
    "Which deals are closing soon?",
]


@pytest.mark.asyncio
async def test_sql_agent_generates_queries():
    model = get_model()
    agent = build_sql_agent(model)

    for question in QUESTIONS:
        sql = await generate_sql(
            agent=agent,
            question=question,
        )

        print("\n" + "=" * 60)
        print("QUESTION:")
        print(question)
        print("\nGENERATED SQL:")
        print(sql)

        assert sql
        assert "SELECT" in sql.upper()