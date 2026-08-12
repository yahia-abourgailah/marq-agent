"""
The `sql_query` tool — the Deals Agent's only path to CRM data.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from app.db.repositories.deals import DealsRepository
from app.sql.agent import Refused, generate_sql
from app.sql.guard import MAX_ROWS, SQLGuardError

# [claude] Serialised-payload ceiling for one tool result, in characters.
# Roughly 5,000 tokens at ~4 chars/token — enough for a wide sample or a few
# hundred narrow rows, while leaving the context room for the conversation
# and the agent's own reasoning.
MAX_RESULT_CHARS = 20_000


def _fit_to_budget(
    rows: list[dict[str, Any]],
    budget: int = MAX_RESULT_CHARS,
) -> list[dict[str, Any]]:
    """
    [claude] Return the longest prefix of `rows` that serialises within
    `budget` characters.

    Rows are dropped from the end rather than columns being trimmed: a
    partial row set is something the agent can reason about and report
    honestly, whereas silently missing columns looks like real data.

    At least one row is always kept, so a single very wide row is still
    visible rather than being swallowed entirely.
    """

    total = 0

    for index, row in enumerate(rows):
        total += len(json.dumps(row, default=str))

        if total > budget:
            return rows[: max(1, index)]

    return rows


class SQLTool:
    """
    Global read-only SQL tool backed by the SQL generation agent.

    The SQL tool:
    - generates SQL through the SQL Agent
    - sends the SQL through the repository and SQL Guard
    - executes the query
    - reports whether the result may have been capped

    Database authorization is handled by PostgreSQL permissions
    and Row-Level Security (RLS).
    """

    def __init__(
        self,
        sql_agent: Any,
        repository: DealsRepository,
    ) -> None:
        self.sql_agent = sql_agent
        self.repository = repository

    async def query(
        self,
        question: str,
    ) -> dict[str, Any]:
        """
        Generate and execute a read-only SQL query.

        The SQL Guard enforces the maximum result size.

        If the number of returned rows reaches MAX_ROWS, `truncated`
        is set to True because the result may have been capped.
        """

        if not question or not question.strip():
            return {
                "success": False,
                "retryable": False,  # [claude] rephrasing will not help
                "error": "The database question cannot be empty.",
            }

        try:
            generated = await generate_sql(
                agent=self.sql_agent,
                question=question,
            )

            # [claude] A refusal is a policy outcome, not a failure. It used
            # to reach the guard as prose, come back as "Invalid SQL query."
            # and send the agent into a retry loop that ended in
            # GraphRecursionError. Reported explicitly, and marked
            # non-retryable so the agent relays it instead of trying again.
            if isinstance(generated, Refused):
                return {
                    "success": False,
                    "retryable": False,
                    "reason": "not_available",
                    "error": generated.reason,
                }

            rows = await self.repository.execute_read(generated.query)

            rows_available = len(rows)

            # [claude] MAX_ROWS bounds the row count but says nothing about
            # row *width*, and width is what actually blows the context
            # window. A full deals row is 62 columns and about 570 tokens,
            # so 500 of them is roughly 285,000 tokens — several times the
            # model's entire context, from one tool call. Even five columns
            # at 500 rows is about 30,000.
            #
            # Cap the serialised payload as well, and say plainly how many
            # rows were kept out of how many matched.
            data = _fit_to_budget(rows)

            # The SQL Guard enforces MAX_ROWS. Receiving exactly MAX_ROWS
            # rows means the result may have been capped there; we cannot
            # tell without a separate COUNT.
            truncated = (
                rows_available >= MAX_ROWS
                or len(data) < rows_available
            )

            return {
                "success": True,
                "data": data,
                "row_count": len(data),
                "rows_available": rows_available,  # [claude]
                "truncated": truncated,
                "max_rows": MAX_ROWS,
            }

        except SQLGuardError as exc:
            # [claude] The generated SQL broke a safety rule. Rephrasing the
            # question can genuinely produce a valid query, so this one is
            # worth another attempt.
            return {
                "success": False,
                "retryable": True,
                "reason": "rejected_by_guard",
                "error": str(exc),
            }

        except Exception as exc:
            # [claude] Everything else — a dead pool, a bad column, a model
            # timeout. Not retryable: the agent used to retry these until it
            # ran out of recursion budget.
            return {
                "success": False,
                "retryable": False,
                "reason": "error",
                "error": type(exc).__name__,
            }


def build_sql_tool(
    sql_agent: Any,
    repository: DealsRepository,
):
    """
    Build the global SQL LangChain tool with injected dependencies.
    """

    service = SQLTool(
        sql_agent=sql_agent,
        repository=repository,
    )

    @tool
    async def sql_query(
        question: str,
    ) -> dict[str, Any]:
        """
        Retrieve CRM data for a natural-language question.

        This is a read-only database retrieval capability.
        """

        return await service.query(question)

    return sql_query


__all__ = [
    "SQLTool",
    "build_sql_tool",
]
