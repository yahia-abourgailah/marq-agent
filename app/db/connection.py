from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings


class Database:
    """Async PostgreSQL connection pool."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        self.dsn = (
            f"host={host} "
            f"port={port} "
            f"user={user} "
            f"password={password} "
            f"dbname={database}"
        )

        self.pool = AsyncConnectionPool(
            conninfo=self.dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={
                "row_factory": dict_row,
            },
        )

    async def connect(self) -> None:
        """Open the connection pool."""
        await self.pool.open()

    async def close(self) -> None:
        """Close the connection pool."""
        await self.pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """Get a connection from the pool."""
        async with self.pool.connection() as conn:
            yield conn


app_db = Database(
    host=settings.postgres_host,
    port=settings.postgres_port,
    user=settings.postgres_user,
    password=settings.postgres_password,
    database=settings.postgres_db,
)


__all__ = ["Database", "app_db"]