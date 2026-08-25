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

import asyncio
import logging
from dataclasses import dataclass

from langgraph.checkpoint.memory import InMemorySaver
from psycopg_pool import AsyncConnectionPool

from app.config import Settings, settings
from app.db.state import build_state_pool

logger = logging.getLogger("marq.graph")


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
    await _setup_once(saver)

    return CheckpointerHandle(
        saver=saver,
        pool=pool,
        # [claude] Only close what we opened. The lifespan shares one pool
        # between the saver and the conversation repository, so a handle
        # closing a borrowed pool would take the repository down with it.
        owns_pool=not borrowed,
    )


async def _setup_once(saver, attempts: int = 5) -> None:
    """
    Create the checkpoint tables, tolerating another process doing it too.

    [claude] `setup()` is idempotent in the sense that matters — it will not
    corrupt anything — and it is **not** safe to run concurrently against a
    database that does not yet have the tables.

    It issues `CREATE TABLE IF NOT EXISTS`, and that is not atomic in
    PostgreSQL: two sessions can both pass the existence check and both try
    to create, and the loser fails with a unique violation on
    `pg_type_typname_nsp_index` rather than a friendly "already exists".

    Which is exactly what happens on first boot with more than one worker.
    Observed in a container with `--workers 2` against an empty volume: both
    workers reached `setup()` in the same instant, one raised, uvicorn saw a
    child fail to start and took the parent down with it. The restart
    succeeded — the tables existed by then — so the symptom is a crash loop
    that heals itself, which is the kind that gets written off as noise.

    This is not a container problem. Any deployment that starts replicas in
    parallel against a fresh database has it, and Kubernetes does that by
    default.

    So: retry a small number of times, and treat "somebody else created it"
    as success rather than as an error. The loop is bounded because a
    genuine permissions failure raises the same class of error, and retrying
    that forever would turn a clear failure into a hang.
    """

    from psycopg import errors

    racy = (
        errors.UniqueViolation,
        errors.DuplicateTable,
        errors.DuplicateObject,
    )

    for attempt in range(1, attempts + 1):
        try:
            await saver.setup()
            return
        except racy:
            if attempt == attempts:
                raise

            logger.info(
                "checkpoint_setup_raced",
                extra={"attempt": attempt, "attempts": attempts},
            )

            # Short, and increasing. The winner needs only to commit; this
            # is waiting for a transaction, not for a service to come up.
            await asyncio.sleep(0.2 * attempt)


__all__ = [
    "CheckpointerHandle",
    "build_checkpointer",
    "open_checkpointer",
]
