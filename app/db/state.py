"""
[claude] The writable pool for application state.

Deliberately separate from `app_db` next door, and the distinction is the
point of the module.

    app_db          CRM reads.        Connects as the read-only role when one
                                      is configured, so a guard bug is a
                                      failed query rather than a write.

    state pool      Our own state.    Conversation checkpoints and the
                                      conversation index. Must write, must
                                      create its own tables.

Handing the second job to the first pool fails in a confusing direction: it
raises a permission error on CREATE TABLE in any environment that configured
`POSTGRES_READONLY_USER` correctly, and works fine on a developer machine
that never set it. The environment doing the right thing is the one that
breaks, which is the worst way for a security control to fail.

Nothing here touches CRM tables. `SQLGuard` remains the only path to those,
and this pool is never handed to it.
"""

from __future__ import annotations

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings, settings


def state_dsn(config: Settings | None = None) -> str:
    """A connection string for the owning role, not the read-only one."""

    config = config or settings

    return make_conninfo(
        host=config.postgres_host,
        port=config.postgres_port,
        user=config.postgres_user,
        password=config.postgres_password,
        dbname=config.postgres_db,
    )


def build_state_pool(
    config: Settings | None = None,
    min_size: int = 1,
    max_size: int = 5,
) -> AsyncConnectionPool:
    """
    Build the application-state pool, unopened.

    The connection kwargs are dictated by LangGraph's AsyncPostgresSaver
    rather than chosen — its own `from_conn_string()` sets exactly these:
    `autocommit` because it manages transactions itself, `prepare_threshold=0`
    because its statements are built per call, and `dict_row` because it
    indexes results by column name.

    The conversation repository shares the pool and is written to suit them,
    which is why it commits nothing explicitly and reads rows as dicts.
    """

    return AsyncConnectionPool(
        conninfo=state_dsn(config),
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )


__all__ = ["build_state_pool", "state_dsn"]
