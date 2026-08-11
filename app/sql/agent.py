from __future__ import annotations

from typing import Any

from langchain.agents import create_agent

from app.sql.catalogue import DEALS_TABLE, DEALS_RULES, DEALS_RELATIONSHIPS, DEALS_ENUMS


SQL_AGENT_PROMPT = """
You are the MarQ SQL Agent.

Your only job is to translate a natural-language data request into
a safe, read-only PostgreSQL query.

You do not execute SQL.
You do not answer the user's question directly.

You only generate the SQL query needed to retrieve the requested data.
- Interpret business terms using the business rules in the catalogue.
- Never assume that "active" simply means "not deleted".
- Always apply the catalogue definition of "active".
- When a business rule requires excluding deleted records, include
  deleted_at IS NULL.

Rules:
- Generate PostgreSQL-compatible SQL.
- Generate read-only queries only.
- Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, or other write operations.
- Use only tables and columns provided in the schema catalogue.
- Never invent tables or columns.
- Use parameterized values where parameters are appropriate.
- Do not expose sensitive fields that are not present in the catalogue.
- Respect the business rules in the catalogue.
- For Deals data, exclude deleted records when the catalogue requires it.
- Prefer precise queries over SELECT *.
- Do not add explanations around the SQL.
- Return only the SQL query.

Deals schema:

TABLE:
{table}

BUSINESS RULES:
{rules}

RELATIONSHIPS:
{relationships}

ENUMS:
{enums}
"""


def build_sql_agent(model: Any):
    """
    Build the global SQL-generation agent.

    The model is injected from the application configuration.
    The agent only generates SQL; execution happens elsewhere.
    """

    system_prompt = SQL_AGENT_PROMPT.format(
        table=DEALS_TABLE,
        rules=DEALS_RULES,
        relationships=DEALS_RELATIONSHIPS,
        enums=DEALS_ENUMS,
    )

    return create_agent(
        model=model,
        tools=[],
        system_prompt=system_prompt,
    )

async def generate_sql(
    agent: Any,
    question: str,
) -> str:
    """Generate one clean SQL query from a natural-language database question."""

    result = await agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": question,
                }
            ]
        }
    )

    messages = result.get("messages", [])

    if not messages:
        raise ValueError("SQL agent returned no messages.")

    content = messages[-1].content

    if isinstance(content, list):
        content = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
        )

    sql = str(content).strip()

    # Remove Markdown code fences if the model adds them.
    if sql.startswith("```"):
        lines = sql.splitlines()

        # Remove opening fence: ```sql / ```
        lines = lines[1:]

        # Remove closing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        sql = "\n".join(lines).strip()

    if not sql:
        raise ValueError("SQL agent returned an empty SQL query.")

    return sql

__all__ = [
    "build_sql_agent",
    "SQL_AGENT_PROMPT",
]