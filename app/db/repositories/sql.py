"""
Read-only database gateway for the Deals domain.

Every query reaches PostgreSQL through here, and every query is validated by
SQLGuard on the way — there is no bypass.
"""

from __future__ import annotations

from typing import Any

from app.sql.executor import SQLExecutor
from app.sql.guard import SQLGuard


class SQLRepository:
    """Read-only database gateway for the Deals domain."""

    def __init__(
        self,
        executor: SQLExecutor,
        guard: SQLGuard,
    ) -> None:
        self.executor = executor
        self.guard = guard

    async def execute_read(
        self,
        query: str,
        params: tuple[Any, ...] = (),
        requester_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Validate and execute one read-only SQL query.

        `requester_id` is passed straight through to the executor, which
        publishes it for row-level security. The repository does not filter
        on it — a filter applied here would be one the agent could be talked
        out of, which is the whole reason it belongs in the database.
        """

        safe_query = self.guard.validate(query)

        return await self.executor.execute(
            safe_query,
            params,
            requester_id=requester_id,
        )


__all__ = ["SQLRepository"]
