"""
Async PostgreSQL connection pooling.

`app_db` is the pool for the Marquise application database. The CRM database
gets its own pool once its credentials are configured.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo  # [claude] see Database.__init__
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
        statement_timeout_ms: int = 10_000,
    ) -> None:
        if statement_timeout_ms <= 0:
            raise ValueError(
                "statement_timeout_ms must be greater than zero."
            )

        # [claude] Was built by f-string interpolation. libpq conninfo is a
        # quoted key=value format, so any credential containing a space, a
        # quote or a backslash produced a malformed DSN — a password like
        # `p@ss word` silently became `dbname=...` garbage and failed to
        # connect with an unhelpful error. make_conninfo does the escaping.
        self.dsn = make_conninfo(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=database,
        )

        self.pool = AsyncConnectionPool(
            conninfo=self.dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={
                "row_factory": dict_row,
                "options": (
                    f"-c statement_timeout={statement_timeout_ms}"
                ),
            },
        )

        # [claude] Guards the lazy open in connection(). The lock is created
        # here rather than at first use so two concurrent tool calls cannot
        # each build one.
        self._opened = False
        self._open_lock = asyncio.Lock()

        # Set by the module-level wiring below; see _READ_ONLY.
        self.is_read_only = False

    async def verify_read_only(self) -> bool:
        """
        Ask the database whether this connection can actually write.

        [claude] Configuration says which role was requested; this says what
        the role can do. The two came apart on the development database,
        where `marq_agent_ro` existed as a login role with no grants at all —
        it looked configured and could read nothing.

        Attempts a write inside a transaction that is always rolled back, so
        it is safe to call on a live database.
        """

        async with self.connection() as conn:
            try:
                async with conn.transaction(force_rollback=True):
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            "CREATE TEMP TABLE _marq_write_probe (x int)"
                        )
            except Exception:
                return True

        return False

    async def connect(self) -> None:
        """Open the connection pool."""
        async with self._open_lock:
            if not self._opened:
                await self.pool.open()
                self._opened = True  # [claude] keep lazy-open state in sync

    async def close(self) -> None:
        """Close the connection pool."""
        async with self._open_lock:
            await self.pool.close()
            self._opened = False  # [claude] allow reopening after close

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """
        Get a connection from the pool, opening the pool on first use.

        [claude] The pool is built with open=False and nothing inside app/
        ever called connect() — only the e2e test and a script did. Any other
        host (LangGraph Studio, a future FastAPI app) therefore hit
        `PoolClosed: the pool is not open yet` on the first data question,
        and SQLTool swallowed it into a generic tool error the agent could
        not act on.

        Opening lazily here makes the pool correct under any host while
        leaving explicit connect()/close() lifecycle management available
        and unchanged for callers that want it.
        """

        if not self._opened:
            async with self._open_lock:
                # Re-check inside the lock: concurrent tool calls race here.
                if not self._opened:
                    await self.pool.open()
                    self._opened = True

        async with self.pool.connection() as conn:
            yield conn


# [claude] Connect as the read-only role when one is configured.
#
# The guard's docstring described PostgreSQL permissions as the authoritative
# layer beneath it, while the application connected as the table owner — so a
# guard bug was a write, not a failed query. With the role configured, the
# database refuses the write regardless of what the guard let through, which
# is what "defence in depth" was supposed to mean.
#
# Falling back to the owner keeps a developer who has not run
# migrations/001_read_only_role.sql working; `is_read_only` says which is in
# force so nothing has to guess.
_READ_ONLY = bool(settings.postgres_readonly_user)

app_db = Database(
    host=settings.postgres_host,
    port=settings.postgres_port,
    user=settings.postgres_readonly_user or settings.postgres_user,
    password=(
        settings.postgres_readonly_password
        if _READ_ONLY
        else settings.postgres_password
    ),
    database=settings.postgres_db,
    statement_timeout_ms=10_000,
)

app_db.is_read_only = _READ_ONLY


__all__ = ["Database", "app_db"]
