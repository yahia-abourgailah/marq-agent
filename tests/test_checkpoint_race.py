"""
[claude] Two processes creating the checkpoint tables at the same moment.

`AsyncPostgresSaver.setup()` issues `CREATE TABLE IF NOT EXISTS`, and that
is not atomic in PostgreSQL: two sessions can both pass the existence check
and both attempt creation, and the loser fails with a unique violation on
`pg_type_typname_nsp_index` rather than a tidy "already exists".

Found by running the container on a fresh volume with two uvicorn workers.
Both reached `setup()` in the same instant, one raised, uvicorn saw a child
fail to start and stopped the parent. The restart succeeded — the tables
existed by then — so it presents as a crash loop that heals itself, which
is the kind nobody investigates.

Not a container problem. Any deployment starting replicas in parallel
against a fresh database has it, and Kubernetes does that by default.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from app.config import settings
from app.db.state import build_state_pool
from app.graph.checkpointer import open_checkpointer

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)


@pytest_asyncio.fixture
async def empty_checkpoint_tables():
    """
    Drop the checkpoint tables, race their creation, and leave them created.

    [claude] Destructive by necessity: the race only exists when the tables
    are absent, so a test that will not drop them cannot reach the bug. It
    runs against the development database, where these hold development
    conversations — and it recreates them, so what is lost is history, not
    the schema.
    """

    pool = build_state_pool(settings)
    await pool.open()

    async def drop():
        async with pool.connection() as conn:
            await conn.set_autocommit(True)
            for table in CHECKPOINT_TABLES:
                await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    await drop()

    yield pool

    await pool.close()


async def test_concurrent_setup_does_not_raise(empty_checkpoint_tables):
    """
    The failure this reproduces. Four callers, one empty database, one
    winner — and three that must treat losing as success.
    """

    pool = empty_checkpoint_tables

    results = await asyncio.gather(
        *(open_checkpointer(settings, pool=pool) for _ in range(4)),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]

    assert not failures, (
        "concurrent setup raised: "
        + "; ".join(f"{type(f).__name__}: {f}" for f in failures)
    )

    for handle in results:
        assert handle.is_persistent


async def test_the_tables_exist_afterwards(empty_checkpoint_tables):
    """
    Tolerating the race must not mean skipping the work. Somebody has to
    have actually created them.
    """

    pool = empty_checkpoint_tables

    await asyncio.gather(
        *(open_checkpointer(settings, pool=pool) for _ in range(3))
    )

    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = ANY(%s)",
                (list(CHECKPOINT_TABLES),),
            )
            found = {row["tablename"] for row in await cursor.fetchall()}

    assert set(CHECKPOINT_TABLES) <= found, f"missing: {set(CHECKPOINT_TABLES) - found}"


async def test_setup_is_still_fine_when_nobody_is_racing(empty_checkpoint_tables):
    """The ordinary path, so the retry cannot mask a plain failure."""

    pool = empty_checkpoint_tables

    first = await open_checkpointer(settings, pool=pool)
    second = await open_checkpointer(settings, pool=pool)

    assert first.is_persistent and second.is_persistent
