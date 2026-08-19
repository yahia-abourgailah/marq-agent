"""
LangGraph checkpointer selection.

[claude] Rewritten for the API layer. `build_checkpointer()` still returns an
InMemorySaver and still behaves exactly as it did, so every existing caller —
the graph builders, the tests, `langgraph dev` — is unchanged. What is new is
`open_checkpointer()`, an async lifecycle the HTTP server uses to get a
PostgreSQL-backed saver that survives a restart.

Why the API layer forced this
-----------------------------
InMemorySaver keeps checkpoints in the running process. That is fine for a
CLI invocation and fatal for an HTTP service, in two separate ways:

*   A restart loses every conversation. Deploys become data loss.
*   Two uvicorn workers do not share memory, so a follow-up question is
    routed by the load balancer to a process that has never heard of the
    thread. The user sees the agent forget mid-conversation, intermittently,
    in a way that reproduces only under more than one worker.

The second is worse because it looks like a model problem rather than an
infrastructure one.
"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.memory import InMemorySaver
from psycopg_pool import AsyncConnectionPool

from app.config import Settings, settings
from app.db.state import build_state_pool


def build_checkpointer() -> InMemorySaver:
    """
    Build the in-process checkpointer used for development and tests.

    InMemorySaver keeps checkpoints in the running process. Suitable for
    local development, the eval suites, and anywhere a single invocation
    starts and finishes in one process.

    The HTTP API uses `open_checkpointer()` instead — see the module
    docstring for why in-process persistence is not enough there.
    """

    return InMemorySaver()


@dataclass
class CheckpointerHandle:
    """
    A checkpointer plus whatever has to be closed with it.

    [claude] Exists because the Postgres saver owns a connection pool and the
    in-memory one owns nothing, and the lifespan should not have to branch on
    which it got.
    """

    saver: object
    pool: AsyncConnectionPool | None = None
    owns_pool: bool = True

    @property
    def is_persistent(self) -> bool:
        """Whether conversations survive a restart. Reported by /health."""

        return self.pool is not None

    async def aclose(self) -> None:
        if self.pool is not None and self.owns_pool:
            await self.pool.close()

        self.pool = None


async def open_checkpointer(
    config: Settings | None = None,
    pool: AsyncConnectionPool | None = None,
) -> CheckpointerHandle:
    """
    Open the checkpointer the HTTP server should use.

    Selected by `settings.checkpointer_backend`:

        "postgres"  persistent, survives restarts and spans workers
        "memory"    in-process; what the tests use

    Postgres is the default. `setup()` is idempotent — it creates the
    checkpoint tables if they are absent and migrates them if they are
    outdated — so calling it on every boot is correct rather than wasteful.

    `pool` lets the caller share the application-state pool rather than open
    a second one. The HTTP lifespan does exactly that, because the
    conversation index needs the same writable connections; a pool passed in
    is not closed by `aclose()`, since this object does not own it.

    Note the pool is *not* `app_db` — that one connects as the read-only
    role, which cannot create the checkpoint tables. See app/db/state.py.
    """

    config = config or settings

    if config.checkpointer_backend != "postgres":
        return CheckpointerHandle(saver=build_checkpointer())

    # Imported here rather than at module scope so that `build_checkpointer()`
    # — which the whole hermetic test suite reaches through the graph
    # builders — never pays for the Postgres saver's import.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    borrowed = pool is not None

    if pool is None:
        pool = build_state_pool(config)
        await pool.open()

    saver = AsyncPostgresSaver(pool)
    await saver.setup()

    return CheckpointerHandle(
        saver=saver,
        pool=pool,
        # [claude] Only close what we opened. The lifespan shares one pool
        # between the saver and the conversation repository, so a handle
        # closing a borrowed pool would take the repository down with it.
        owns_pool=not borrowed,
    )


__all__ = [
    "CheckpointerHandle",
    "build_checkpointer",
    "open_checkpointer",
]
