"""
The SQL Agent — turns a natural-language question into one read-only query.

It only generates SQL. Validating it is SQLGuard's job; running it is
SQLExecutor's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent

from app.sql.catalogue import (
    DEALS_ENUMS,
    DEALS_RELATIONSHIPS,
    DEALS_RULES,
    DEALS_TABLE,
    LEADS_RELATIONSHIPS,
    LEADS_TABLE,
    USERS_TABLE,
    Table,
    render_tables,
)

# [claude] Sentinel the SQL Agent emits when a request cannot be served from
# the catalogue. A prefix is used rather than sniffing prose so the refusal
# path is deterministic.
REFUSAL_PREFIX = "CANNOT_ANSWER:"

DEFAULT_REFUSAL = (
    "That information is not available through this agent."
)


@dataclass(frozen=True)
class Sql:
    """[claude] The agent produced a query."""

    query: str


@dataclass(frozen=True)
class Refused:
    """[claude] The agent declined — a policy outcome, not an error."""

    reason: str


SqlResult = Sql | Refused

SQL_AGENT_PROMPT = """
You are the MarQ SQL Agent.

Your only job is to translate a natural-language data request into
one safe, read-only PostgreSQL query.

You do not execute SQL.
You do not answer the user's question directly.

You ONLY generate SQL.

IMPORTANT:
- Never return explanations.
- Never return comments.
- Never return apologies.
- Do not use Markdown code fences.

You return exactly one of two things, and nothing else.

1. A SQL SELECT query, when the request can be answered from the
   catalogue. Return the query alone, with no surrounding text.

2. The literal prefix CANNOT_ANSWER: followed by one short sentence,
   when the request cannot be answered from the catalogue. For example:

       CANNOT_ANSWER: that information is not available through this agent.

   Use this — and only this — when the request requires a restricted
   column, or a table, column or business definition the catalogue does
   not provide.

Never mix the two. Never wrap either in prose.

Rules:

1. Generate PostgreSQL-compatible SQL.

2. Generate exactly ONE read-only SELECT query.

3. Use only tables and columns explicitly provided in the catalogue.

4. Never invent tables or columns.

5. Use the relationships provided in the catalogue when a question
   requires related data.

6. Interpret business terminology using the business rules.

7. Always apply the catalogue definition of "active".

8. When the catalogue requires deleted records to be excluded,
   include:
       deleted_at IS NULL

9. When the user asks for a count:
       COUNT(*)

10. When the user asks for total deal value:
       SUM(value)

11. When the user asks for an average:
       AVG(...)

12. When the user asks for deals by owner, use the owner relationship
    provided by the catalogue if the required user table is available.

13. When the user asks for deals by project, use the project relationship
    provided by the catalogue if the required project table is available.

14. Never fabricate missing schema information.

15. Never use:
    INSERT
    UPDATE
    DELETE
    DROP
    ALTER
    CREATE
    TRUNCATE
    MERGE
    GRANT
    REVOKE

16. Do not generate SQL that modifies database state.

17. Prefer precise columns instead of SELECT * when possible.

18. Return ONLY the SQL query.
RESTRICTED COLUMNS:

Some database columns exist in the underlying CRM but are restricted
from this agent.

The agent may know that these columns exist, but MUST NEVER use them
in generated SQL.

Restricted columns include:

- unit_price
- reservation_price
- contract_price
- collection_price
- down_payment
- total_retroactive_commission

If a user asks for information requiring a restricted column:

- Do not generate SQL using that column.
- Do not substitute an invented column.
- Do not attempt to bypass the restriction.
- Reply with the CANNOT_ANSWER: form described above.
SCHEMA:
{tables}

BUSINESS RULES:
{rules}

RELATIONSHIPS:
{relationships}

ENUMS:
{enums}
"""


# [claude] The tables the SQL Agent may query. This must stay aligned with
# the SQLGuard table allowlist, which derives from get_deals_catalogue().
PROMPT_TABLES = (DEALS_TABLE, LEADS_TABLE, USERS_TABLE)


def build_sql_agent(model: Any, tables: Sequence[Table] | None = None):
    """
    Build the global SQL-generation agent.

    The model is injected from the application configuration.
    The agent only generates SQL; execution happens elsewhere.

    [claude] Two changes here:

    - The schema block is rendered with render_tables() instead of
      interpolating the dataclass, which drops the repeated
      `Column(name=..., description=...)` scaffolding.

    - `leads` and `users` are now included. They never were, even though
      business rules 13, 16 and 28 instruct the model to join them — so it
      was told to use `users.name` and `leads.merged_into_id` without ever
      being shown those tables, and had to guess the column names.

    `tables` is a parameter so a caller can narrow the schema block to the
    tables a given question actually needs.
    """

    if tables is None:
        tables = PROMPT_TABLES

    system_prompt = SQL_AGENT_PROMPT.format(
        tables=render_tables(tables),
        rules=DEALS_RULES,
        relationships=DEALS_RELATIONSHIPS + LEADS_RELATIONSHIPS,
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
) -> SqlResult:
    """
    Turn a natural-language question into SQL, or into a refusal.

    [claude] This used to return a bare `str`, which gave a refusal nowhere
    to go. The prompt told the model both to "never say that the request
    cannot be resolved" and to "return a concise statement that the
    requested information is not available" — and when it chose the second,
    the prose went to SQLGuard, failed to parse, and surfaced as
    `{"success": false, "error": "Invalid SQL query."}`.

    The Deals Agent read that as malformed SQL, retried, and burned its
    recursion budget until the run died with GraphRecursionError. Asking for
    a masked column — the case that most needs a clean answer — was the case
    that crashed.

    Returning a union makes the two outcomes distinguishable by type, so a
    policy refusal can be relayed to the user instead of looking like a bug.
    """

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

    # [claude] The prompt asks for this exact prefix when the request cannot
    # be served from the catalogue.
    if sql.upper().startswith(REFUSAL_PREFIX):
        return Refused(reason=sql[len(REFUSAL_PREFIX):].strip() or DEFAULT_REFUSAL)

    # [claude] Safety net. If the model ignores the contract and answers in
    # prose anyway, treat it as a refusal rather than feeding prose to the
    # guard and reporting a syntax error. Every statement the guard accepts
    # begins with SELECT or WITH.
    if not sql.upper().lstrip("( \n\t").startswith(("SELECT", "WITH")):
        return Refused(reason=DEFAULT_REFUSAL)

    return Sql(query=sql)


__all__ = [
    "DEFAULT_REFUSAL",  # [claude]
    "REFUSAL_PREFIX",  # [claude]
    "SQL_AGENT_PROMPT",
    "Refused",  # [claude]
    "Sql",  # [claude]
    "SqlResult",  # [claude]
    "build_sql_agent",
    # [claude] generate_sql was missing from __all__ despite being the
    # module's main entry point and imported by app/tools/sql.py.
    "generate_sql",
]
