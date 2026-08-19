"""
[claude] The persistence layer against a real PostgreSQL.

Marked `integration` because it needs a live database — `pytest` stays
hermetic and `pytest -m integration` runs these.

They exist because the hermetic API tests use `FakeConversations` and a stub
saver, which prove the routes call the right methods with the right scoping
and prove nothing about the SQL underneath. The two failures that matter here
are invisible to a fake:

*   an upsert that reorders a user's list on every turn,
*   an ownership filter that is applied in Python rather than in the WHERE
    clause, which works until two users pick the same thread id.

Every row these create is namespaced with a `pytest-` subject and removed in
the fixture teardown, so a shared development database is not polluted.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.auth.principal import Principal
from app.db.repositories.conversations import (
    ConversationRepository,
    make_title,
)
from app.db.state import build_state_pool
from app.graph.checkpointer import open_checkpointer

pytestmark = pytest.mark.integration


# [claude] pytest_asyncio.fixture, not pytest.fixture. Under
# asyncio_mode = "strict" a plain @pytest.fixture on an async generator
# is handed to the test unawaited, and every test in the module errors
# during setup rather than failing with anything readable.
@pytest_asyncio.fixture
async def pool():
    pool = build_state_pool()
    await pool.open()

    yield pool

    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM conversations WHERE subject LIKE 'pytest-%'"
        )

    await pool.close()


@pytest.fixture
def subjects():
    """Two distinct subjects, unique per run so parallel runs cannot clash."""

    run = uuid.uuid4().hex[:8]

    return f"pytest-alice-{run}", f"pytest-bob-{run}"


@pytest.mark.asyncio
async def test_the_conversations_table_exists(pool):
    """
    migrations/003_conversations.sql has been applied to this database.

    A failure here is a missing migration, not a code bug — worth saying
    plainly, because the symptom otherwise is every thread endpoint 500ing.
    """

    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'conversations'
                """
            )
            columns = {row["column_name"] for row in await cursor.fetchall()}

    assert {
        "thread_key",
        "subject",
        "thread_id",
        "title",
        "created_at",
        "updated_at",
        "turn_count",
    } <= columns


@pytest.mark.asyncio
async def test_two_employees_may_use_the_same_thread_id(pool, subjects):
    """
    The collision case, against the real unique constraint.

    Both users naming a thread "today" is ordinary — and if ownership were
    enforced anywhere other than the key, one would overwrite the other.
    """

    alice, bob = subjects
    repository = ConversationRepository(pool)

    await repository.record_turn(
        alice, "today", Principal(alice).thread_key("today"), "alice asked"
    )
    await repository.record_turn(
        bob, "today", Principal(bob).thread_key("today"), "bob asked"
    )

    assert (await repository.get(alice, "today")).title == "alice asked"
    assert (await repository.get(bob, "today")).title == "bob asked"


@pytest.mark.asyncio
async def test_a_thread_is_not_readable_by_another_employee(pool, subjects):
    alice, bob = subjects
    repository = ConversationRepository(pool)

    await repository.record_turn(
        alice, "private", Principal(alice).thread_key("private"), "secret"
    )

    assert await repository.get(bob, "private") is None
    assert await repository.delete(bob, "private") is False
    # Still Alice's, untouched.
    assert await repository.get(alice, "private") is not None


@pytest.mark.asyncio
async def test_turns_accumulate_without_renaming_the_conversation(pool, subjects):
    """
    The title is the question that started it.

    Rewriting it every turn would make the list reorder itself under the
    user as they type.
    """

    alice, _ = subjects
    repository = ConversationRepository(pool)
    key = Principal(alice).thread_key("t")

    await repository.record_turn(alice, "t", key, make_title("first question"))

    for _ in range(3):
        await repository.record_turn(alice, "t", key, make_title("later one"))

    row = await repository.get(alice, "t")

    assert row.turn_count == 4
    assert row.title == "first question"


@pytest.mark.asyncio
async def test_listing_is_ordered_by_recency_and_scoped(pool, subjects):
    alice, bob = subjects
    repository = ConversationRepository(pool)

    for thread_id in ("one", "two", "three"):
        await repository.record_turn(
            alice, thread_id, Principal(alice).thread_key(thread_id), thread_id
        )

    await repository.record_turn(
        bob, "bobs", Principal(bob).thread_key("bobs"), "bobs"
    )

    listed = await repository.list_for(alice)

    assert [row.thread_id for row in listed] == ["three", "two", "one"]
    assert [row.thread_id for row in await repository.list_for(bob)] == ["bobs"]


@pytest.mark.asyncio
async def test_a_deleted_conversation_stays_deleted(pool, subjects):
    alice, _ = subjects
    repository = ConversationRepository(pool)

    await repository.record_turn(
        alice, "gone", Principal(alice).thread_key("gone"), "t"
    )

    assert await repository.delete(alice, "gone") is True
    assert await repository.delete(alice, "gone") is False
    assert await repository.get(alice, "gone") is None


@pytest.mark.asyncio
async def test_the_postgres_checkpointer_round_trips(pool):
    """
    A checkpoint written through the saver is really in PostgreSQL.

    [claude] Verified with a direct SQL count rather than by reading it back
    through the saver, which would pass just as well against an in-process
    cache. docs/HANDOFF.md: verify against SQL, not against "it didn't
    crash".
    """

    from langgraph.checkpoint.base import empty_checkpoint

    handle = await open_checkpointer(pool=pool)
    thread_key = f"pytest-{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": thread_key, "checkpoint_ns": ""}}

    try:
        await handle.saver.aput(
            config, empty_checkpoint(), {"source": "input", "step": -1}, {}
        )

        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s",
                    (thread_key,),
                )
                stored = (await cursor.fetchone())["n"]

        assert stored >= 1
        assert handle.is_persistent
        assert await handle.saver.aget_tuple(config) is not None
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = %s", (thread_key,)
            )
        await handle.aclose()


@pytest.mark.asyncio
async def test_a_borrowed_pool_is_not_closed_by_the_handle(pool):
    """
    The lifespan shares one pool between the saver and the repository, so a
    handle closing a pool it borrowed would take the repository down with it.
    """

    handle = await open_checkpointer(pool=pool)

    await handle.aclose()

    # Still usable.
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT 1 AS ok")
            assert (await cursor.fetchone())["ok"] == 1
