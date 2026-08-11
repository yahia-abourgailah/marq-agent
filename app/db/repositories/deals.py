from __future__ import annotations

from typing import Any

from app.sql.executor import SQLExecutor
from app.sql.guard import SQLGuard


class DealsRepository:
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
    ) -> list[dict[str, Any]]:
        """Validate and execute one read-only SQL query."""

        safe_query = self.guard.validate(query)

        return await self.executor.execute(
            safe_query,
            params,
        )


__all__ = ["DealsRepository"]