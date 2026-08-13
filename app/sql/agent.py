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
    DEALS_TABLE,
    LEADS_TABLE,
    USERS_TABLE,
    Table,
    build_rules,
    relationships_for,
    render_enums,
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

# [claude] Rewritten.
#
# The previous prompt carried 18 numbered instructions plus a RESTRICTED
# COLUMNS section, then concatenated the 43 catalogue rules underneath —
# and the two overlapped heavily. Four instructions forbade DML that
# SQLGuard already rejects, the masked-column list appeared twice, and
# instruction 10 said "total deal value: SUM(value)" for a `value` column
# that does not exist in this schema at all, actively teaching the model to
# hallucinate it.
#
# What remains here is only the agent's role and its output contract. All
# domain knowledge now lives in the catalogue rules, in one place.
SQL_AGENT_PROMPT = """\
You are the MarQ SQL Agent. You turn one natural-language question into one
read-only PostgreSQL SELECT query, using only the schema and rules below.

You never execute SQL and you never answer the question in prose.

OUTPUT
Return exactly one of the following, with nothing before or after it — no
explanation, no apology, no commentary, no markdown fences:

  1. A single SELECT query.

  2. CANNOT_ANSWER: <one short sentence>

CHOOSING BETWEEN THEM

Work through these two steps in order. Do not skip to CANNOT_ANSWER.

STEP 1 — Cross out what you do not have.
Take the request and delete every field that has no column in SCHEMA, and
every field that is restricted. Never mention the deletion. What is left is
the question you answer.

    "the 5 deals closing soonest, with the floor number and the closing
    date"
      no floor column, so cross it out
      what remains: "the 5 deals closing soonest, with the closing date"

STEP 2 — Is anything left to measure or list?

  Yes -> return SQL for what remains.
         SELECT id, unit_number, expected_closing_date FROM deals ...
         This is the correct answer. It is NOT a refusal, even though part
         of the request was crossed out.

  No  -> return CANNOT_ANSWER, because the whole subject was crossed out.
         "the total contract price" leaves nothing behind: contract_price
         is restricted and there is no other price column.
         -> CANNOT_ANSWER: that information is not available.

Never invent a column, and never answer a question about one column by
quietly substituting another.

SCHEMA
{tables}

RULES
{rules}

RELATIONSHIPS
{relationships}

ENUMS
{enums}

REMINDER
Cross out the fields you do not have, then answer what remains. Only return
CANNOT_ANSWER when nothing is left to measure or list.
"""


# [claude] The tables the SQL Agent may query. This must stay aligned with
# the SQLGuard table allowlist, which derives from get_catalogue().
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

    `tables` narrows the agent's whole query surface: the schema block, the
    rules, and the relationships all follow it. Narrowing the schema while
    still sending every rule would describe tables the agent cannot see.

    The SQLGuard for this agent must be given the same table set, or the
    guard will permit a surface the prompt never described.
    """

    if tables is None:
        tables = PROMPT_TABLES

    names = [table.name for table in tables]

    # [claude] Rendered as lines, not interpolated as Python containers —
    # a tuple or dict repr spends tokens on quotes, commas and brackets that
    # carry no meaning for the model.
    system_prompt = SQL_AGENT_PROMPT.format(
        tables=render_tables(tables),
        rules=build_rules(names),
        relationships="\n".join(
            f"  {relationship}" for relationship in relationships_for(names)
        ),
        enums=render_enums(),
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
    "PROMPT_TABLES",  # [claude]
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
