from __future__ import annotations

import pytest

from app.llm.model import get_model
from app.sql.agent import Refused, Sql, build_sql_agent, generate_sql

# [claude] Marked as integration: this module calls the live model
# endpoint for every question.
# Excluded from the default run (see pyproject.toml); use
#     pytest -m integration
pytestmark = pytest.mark.integration


QUESTIONS = [
    "How many active deals do we have?",
    "How many contracted deals do we have?",
    "How many reservation deals do we have?",
    "How many EOI deals do we have?",
    "How many cancelled deals do we have?",
    "How many deals does Sara Mostafa own?",
    "Which deals are closing soon?",
    "How many deals are commercial?",
]


FORBIDDEN_COLUMNS = [
    "unit_price",
    "reservation_price",
    "contract_price",
    "collection_price",
    "down_payment",
    "total_retroactive_commission",
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
        assert isinstance(sql, Sql), f"expected SQL, got {sql!r}"
        sql = sql.query

        print("\n" + "=" * 60)
        print("QUESTION:")
        print(question)
        print("\nGENERATED SQL:")
        print(sql)

        assert sql
        assert "SELECT" in sql.upper()

        upper_sql = sql.upper()

        for column in FORBIDDEN_COLUMNS:
            assert column.upper() not in upper_sql


@pytest.mark.asyncio
async def test_sql_agent_owner_query_uses_users():
    model = get_model()
    agent = build_sql_agent(model)

    sql = await generate_sql(
        agent=agent,
        question="How many deals does Sara Mostafa own?",
    )
    assert isinstance(sql, Sql), f"expected SQL, got {sql!r}"
    sql = sql.query

    print("\n" + "=" * 60)
    print("OWNER QUERY:")
    print(sql)

    upper_sql = sql.upper()

    assert "SELECT" in upper_sql
    assert "DEALS" in upper_sql
    assert "USERS" in upper_sql
    assert "OWNER_ID" in upper_sql
    assert "NAME" in upper_sql

    for column in FORBIDDEN_COLUMNS:
        assert column.upper() not in upper_sql


@pytest.mark.asyncio
async def test_sql_agent_never_generates_write_queries():
    model = get_model()
    agent = build_sql_agent(model)

    question = "Show me the active deals."

    sql = await generate_sql(
        agent=agent,
        question=question,
    )
    assert isinstance(sql, Sql), f"expected SQL, got {sql!r}"
    sql = sql.query

    upper_sql = sql.upper()

    assert "SELECT" in upper_sql

    forbidden_operations = [
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "MERGE",
        "GRANT",
        "REVOKE",
    ]

    for operation in forbidden_operations:
        assert not upper_sql.lstrip().startswith(operation)


@pytest.mark.asyncio
async def test_sql_agent_does_not_use_masked_money_columns():
    model = get_model()
    agent = build_sql_agent(model)

    questions = [
        "What is the total value of active deals?",
        "What is the total contract price?",
        "What is the total reservation price?",
        "How much down payment has been collected?",
    ]

    for question in questions:
        result = await generate_sql(
            agent=agent,
            question=question,
        )

        print("\n" + "=" * 60)
        print("MASKED DATA QUESTION:")
        print(question)
        print("\nRESULT:")
        print(result)

        # [claude] Either outcome is correct here, and which one the model
        # picks is not the point of this test:
        #
        #   Refused  the agent declined, which is the intended behaviour
        #            for a masked column
        #   Sql      the agent answered from permitted columns only
        #
        # What must never happen is a masked column appearing in generated
        # SQL. Previously this asserted on a bare string, so a refusal and a
        # query were indistinguishable and the refusal path went untested.
        if isinstance(result, Refused):
            assert result.reason
            continue

        upper_sql = result.query.upper()

        for column in FORBIDDEN_COLUMNS:
            assert column.upper() not in upper_sql
