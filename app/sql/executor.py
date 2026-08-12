"""
Execution of validated SQL against PostgreSQL.
"""

from __future__ import annotations

from typing import Any

from app.db.connection import Database


class SQLExecutor:
    """Execute read-only SQL queries against PostgreSQL."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def execute(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        """
        Execute one SQL query and return result rows.

        The database connection uses the dedicated PostgreSQL
        read-only role. SQLGuard validates the query before it
        reaches this executor.

        `params or None` is important for psycopg:
        an empty tuple causes psycopg to treat `%` characters inside
        SQL string literals as parameter placeholders.
        """

        async with self.database.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    query,
                    params or None,
                )

                return await cursor.fetchall()


__all__ = ["SQLExecutor"]
