from __future__ import annotations

from typing import Any

from app.db.connection import Database


class SQLExecutor:
    """Execute SQL queries against PostgreSQL."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def execute(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        """Execute a query and return result rows as dictionaries."""

        async with self.database.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, params)
                return await cursor.fetchall()


__all__ = ["SQLExecutor"]