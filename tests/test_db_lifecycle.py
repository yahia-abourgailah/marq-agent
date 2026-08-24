"""
[claude] Pool and checkpointer lifecycle.

Written after the fact, which is the point. `Database.close()` set
`_opened = False` under a comment reading "allow reopening after close" while
the psycopg pool underneath stayed permanently dead, so `connect()` afterwards
raised `PoolClosed`. Nothing caught it because `app_db` is a module-level
singleton that production opens exactly once — it surfaced only when the API
boot test started the application twice in one process.

The fix went in with no regression test, which is how a fix comes back. These
are that test.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.config import reveal, settings
from app.db.connection import Database
from app.db.state import build_state_pool, state_dsn
from app.graph.checkpointer import (
    CheckpointerHandle,
    build_checkpointer,
    open_checkpointer,
)

# ============================================================
# Hermetic — no database needed
# ============================================================


def test_a_fresh_pool_is_built_each_time():
    """
    `_build_pool` returns a new object rather than reconfiguring one.

    A psycopg pool is single-use, so "rebuild" has to mean a genuinely new
    instance. Returning the same object would satisfy the call site and fix
    nothing.
    """

    database = Database(
        host="localhost", port=5432, user="u", password="p", database="d"
    )

    first = database.pool
    second = database._build_pool()

    assert first is not second

    # [claude] The first version asserted `second.closed is False or
    # second.closed is True`, which cannot fail. Assert what actually
    # matters instead: the rebuilt pool carries the same connection details,
    # so a reconnect reaches the same database rather than a default one.
    assert second.conninfo == first.conninfo
    assert second.kwargs == first.kwargs


def test_the_state_pool_uses_the_owning_role_not_the_read_only_one():
    """
    [claude] The distinction the whole of app/db/state.py exists for.

    `app_db` connects as `postgres_readonly_user` when one is configured,
    and that role cannot create the checkpoint tables. A state pool built
    from the same credentials would fail on any environment that configured
    itself correctly and work on a developer machine that had not — the
    security posture and the deployment punished for it exactly inverted.
    """

    dsn = state_dsn(settings)

    assert f"user={settings.postgres_user}" in dsn

    if settings.postgres_readonly_user:
        assert settings.postgres_readonly_user not in dsn


def test_the_state_pool_carries_the_kwargs_the_saver_requires():
    """
    All three are dictated by AsyncPostgresSaver, not chosen. Its own
    `from_conn_string` sets exactly these, and the conversation repository
    is written to suit them.
    """

    pool = build_state_pool(settings)

    assert pool.kwargs["autocommit"] is True
    assert pool.kwargs["prepare_threshold"] == 0
    assert pool.kwargs["row_factory"] is not None


@pytest.mark.asyncio
async def test_the_memory_backend_opens_no_pool():
    """
    `CHECKPOINTER_BACKEND=memory` must not reach PostgreSQL at all — it is
    what keeps the tests and the eval suites hermetic.
    """

    handle = await open_checkpointer(
        settings.model_copy(update={"checkpointer_backend": "memory"})
    )

    assert handle.pool is None
    assert handle.is_persistent is False
    assert type(handle.saver) is type(build_checkpointer())

    # Closing one that owns nothing is a no-op rather than an error.
    await handle.aclose()


@pytest.mark.asyncio
async def test_closing_a_handle_that_borrowed_a_pool_leaves_it_open():
    """
    The lifespan shares one pool between the saver and the conversation
    repository. A handle closing a pool it borrowed would take the
    repository down with it.
    """

    class FakePool:
        def __init__(self):
            self.closed_called = False

        async def close(self):
            self.closed_called = True

    pool = FakePool()
    handle = CheckpointerHandle(saver=object(), pool=pool, owns_pool=False)

    await handle.aclose()

    assert pool.closed_called is False


@pytest.mark.asyncio
async def test_closing_a_handle_that_owns_its_pool_closes_it():
    class FakePool:
        def __init__(self):
            self.closed_called = False

        async def close(self):
            self.closed_called = True

    pool = FakePool()
    handle = CheckpointerHandle(saver=object(), pool=pool, owns_pool=True)

    await handle.aclose()

    assert pool.closed_called is True


# ============================================================
# Against a real database
# ============================================================


@pytest_asyncio.fixture
async def database():
    db = Database(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=reveal(settings.postgres_password),
        database=settings.postgres_db,
    )

    yield db

    await db.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_pool_can_be_closed_and_reopened(database):
    """
    [claude] The regression test for the bug the API boot path found.

    Three full cycles, each running a real query, because the failure was
    not "close raises" — close worked fine. It was that the *next* connect
    raised `PoolClosed` on a pool that could never be reopened, while
    `_opened = False` claimed otherwise.
    """

    for _ in range(3):
        await database.connect()

        async with database.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1 AS ok")
                assert (await cursor.fetchone())["ok"] == 1

        await database.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_lazy_path_also_survives_a_close(database):
    """
    `connection()` opens the pool on first use without `connect()` — the
    path LangGraph Studio and any other host takes. It rebuilds too, or the
    fix would only work for callers that manage the lifecycle explicitly.
    """

    async with database.connection() as conn:
        await conn.execute("SELECT 1")

    await database.close()

    # No connect() this time: straight back through the lazy path.
    async with database.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT 2 AS ok")
            assert (await cursor.fetchone())["ok"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_closing_twice_is_harmless(database):
    """Shutdown paths run more than once when something else has failed."""

    await database.connect()
    await database.close()
    await database.close()
