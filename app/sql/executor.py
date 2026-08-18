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
        requester_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute one SQL query and return result rows.

        SQLGuard validates the query before it reaches this executor, and
        the connection uses the read-only role when one is configured.

        `params or None` is important for psycopg: an empty tuple causes
        psycopg to treat `%` characters inside SQL string literals as
        parameter placeholders.

        [claude] `requester_id` is the employee the question is being asked
        on behalf of. It is published to the session as `app.requester_id`
        so a row-level security policy can filter on it.

        Three things about how it is set:

        *   `SET LOCAL` inside an explicit transaction, so it is scoped to
            this statement and cannot leak to the next borrower of a pooled
            connection. A pool makes session state genuinely dangerous —
            without LOCAL, one user's identity would answer another user's
            question.

        *   Passed as a bound parameter through `set_config`, never
            interpolated. `SET LOCAL x = '...'` takes no parameters, so the
            value would have to be formatted into the string; `set_config`
            is the function form that accepts one.

        *   Omitted entirely when None, rather than set to an empty string.

            Note what "absent" actually looks like, because it is not what
            it first appears. On a connection that has never carried the
            setting, `current_setting('app.requester_id', true)` returns
            NULL. Once any transaction has set it, PostgreSQL keeps the GUC
            defined for the rest of the session and rolling back returns it
            to '' rather than removing it — so on a *pooled* connection,
            which is every connection here, absent reads as the empty
            string.

            A policy must therefore treat NULL and '' identically:

                NULLIF(current_setting('app.requester_id', true), '')

            A policy testing `IS NULL` alone would pass on a fresh
            connection and fail on a reused one, which is the worst possible
            distribution of a security check. See
            migrations/002_row_level_security.sql, which uses the form
            above.

        NOTE: no RLS policy consumes this yet — see
        migrations/002_row_level_security.sql. Until that is applied this
        publishes an identity nothing reads, which is deliberate: the
        plumbing has to exist before the policy can be switched on safely.
        """

        async with self.database.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    if requester_id is not None:
                        await cursor.execute(
                            "SELECT set_config('app.requester_id', %s, true)",
                            (str(requester_id),),
                        )

                    await cursor.execute(
                        query,
                        params or None,
                    )

                    return await cursor.fetchall()


__all__ = ["SQLExecutor"]
