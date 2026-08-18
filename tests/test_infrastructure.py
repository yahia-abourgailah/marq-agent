"""
[claude] The two modules the hermetic suite cannot reach.

`app/db/connection.py` and `app/workspace/embeddings.py` sat at 59% because
their uncovered lines are the real thing: opening a pool, loading a 470MB
encoder. Faking either proves nothing — the failure modes worth catching are
a pool that will not open and a model whose vectors are the wrong width.

Marked integration, so they run with `pytest -m integration` alongside the
other live tests and never slow the hermetic suite.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# ============================================================
# The database pool
# ============================================================


@pytest.mark.asyncio
async def test_the_pool_opens_and_answers():
    from app.db.connection import app_db

    await app_db.connect()

    async with app_db.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1 AS ok")
            row = await cursor.fetchone()

    value = next(iter(row.values())) if hasattr(row, "values") else row[0]

    assert value == 1


@pytest.mark.asyncio
async def test_connecting_twice_is_safe():
    """
    The pool is a module-level singleton, so a second connect must be a
    no-op rather than opening a second pool or raising.
    """

    from app.db.connection import app_db

    await app_db.connect()
    await app_db.connect()

    async with app_db.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")


@pytest.mark.asyncio
async def test_a_bad_query_raises_without_killing_the_pool():
    """
    A failed statement must not poison the pool for the next caller — the
    agent writes invalid SQL routinely and the guard cannot catch everything.
    """

    import psycopg

    from app.db.connection import app_db

    await app_db.connect()

    with pytest.raises(psycopg.Error):
        async with app_db.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT no_such_column FROM deals")

    # Still usable.
    async with app_db.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT count(*) FROM deals")
            assert await cursor.fetchone()


@pytest.mark.asyncio
async def test_the_executor_runs_a_real_query():
    from app.db.connection import app_db
    from app.sql.executor import SQLExecutor

    rows = await SQLExecutor(database=app_db).execute(
        "SELECT status FROM deals WHERE deleted_at IS NULL LIMIT 3"
    )

    assert len(rows) <= 3
    assert all("status" in row for row in rows)


# ============================================================
# The real embedding model
# ============================================================


@pytest.fixture(scope="module")
def embedder():
    from app.workspace.embeddings import SentenceTransformerEmbedder

    return SentenceTransformerEmbedder()


def test_the_configured_model_loads_and_reports_its_width(embedder):
    """
    The declared dimension is used to create the Qdrant collection before
    the model has loaded. If they disagree, every upsert fails with a
    dimension mismatch far from the cause.
    """

    from app.config import settings
    from app.workspace.embeddings import DEFAULT_EMBEDDING_DIMENSION

    assert embedder.model_name == settings.embedding_model

    vector = embedder.embed_query("a warm-up sentence")

    assert len(vector) == DEFAULT_EMBEDDING_DIMENSION
    assert embedder.dimension == DEFAULT_EMBEDDING_DIMENSION


def test_vectors_come_back_normalised(embedder):
    """
    The collection uses cosine distance on the assumption that the encoder
    normalises. If it stops, scores stay plausible and rankings quietly
    change.
    """

    vector = embedder.embed_query("payment terms and penalties")

    magnitude = sum(value * value for value in vector) ** 0.5

    assert magnitude == pytest.approx(1.0, abs=1e-3)


def test_the_same_text_always_embeds_identically(embedder):
    assert embedder.embed_query("identical") == embedder.embed_query("identical")


def test_related_text_scores_higher_than_unrelated(embedder):
    """
    The one real semantic assertion in the suite. The fake embedder cannot
    make it, and it is the property every workspace search depends on.
    """

    query = embedder.embed_query("what is the late payment penalty?")

    related = embedder.embed_query(
        "Late payment incurs a penalty of two percent per month."
    )
    unrelated = embedder.embed_query(
        "Handover is expected in the fourth quarter."
    )

    def similarity(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert similarity(query, related) > similarity(query, unrelated)


def test_the_model_is_multilingual(embedder):
    """
    The reason this model was chosen over an English-only encoder: the CRM
    carries Arabic and English, and an Arabic question must find English
    source text.
    """

    arabic = embedder.embed_query("ما هي غرامة التأخير في السداد؟")

    related = embedder.embed_query(
        "Late payment incurs a penalty of two percent per month."
    )
    unrelated = embedder.embed_query("The unit has three bedrooms.")

    def similarity(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert similarity(arabic, related) > similarity(arabic, unrelated)


def test_embedding_nothing_returns_nothing(embedder):
    assert embedder.embed_documents([]) == []


# ============================================================
# [claude] Requester identity reaching PostgreSQL.
#
# The plumbing an RLS policy will filter on. Verified against a real pool
# because the property that matters — that the value does not survive into
# the next borrower of a pooled connection — cannot be observed with a fake.
# ============================================================


@pytest.mark.asyncio
async def test_the_requester_id_reaches_the_session():
    from app.db.connection import app_db
    from app.sql.executor import SQLExecutor

    rows = await SQLExecutor(database=app_db).execute(
        "SELECT current_setting('app.requester_id', true) AS who",
        requester_id="user-42",
    )

    assert rows[0]["who"] == "user-42"


@pytest.mark.asyncio
async def test_the_requester_id_does_not_leak_to_the_next_query():
    """
    The reason it is SET LOCAL inside a transaction. Without that, a pooled
    connection would carry one employee's identity into the next employee's
    question — and an RLS policy would happily filter on it.
    """

    from app.db.connection import app_db
    from app.sql.executor import SQLExecutor

    executor = SQLExecutor(database=app_db)

    await executor.execute("SELECT 1", requester_id="user-42")

    rows = await executor.execute(
        "SELECT current_setting('app.requester_id', true) AS who"
    )

    assert rows[0]["who"] in (None, "")


@pytest.mark.asyncio
async def test_absent_reads_as_null_or_empty_and_a_policy_must_accept_both():
    """
    [claude] This test was written asserting NULL and failed against a real
    pool, which is exactly why it is here rather than against a fake.

    On a connection that has never carried the setting, current_setting
    returns NULL. Once any transaction has set it, PostgreSQL keeps the GUC
    defined for the session and a rollback returns it to '' — so on a pooled
    connection, absent reads as the empty string. A policy testing IS NULL
    alone would pass on a fresh connection and fail on a reused one.

    NULLIF(..., '') collapses both, and is the form the RLS migration uses.
    """

    from app.db.connection import app_db
    from app.sql.executor import SQLExecutor

    executor = SQLExecutor(database=app_db)

    # Ensure the session has carried it at least once — the pooled case.
    await executor.execute("SELECT 1", requester_id="user-42")

    rows = await executor.execute(
        "SELECT NULLIF(current_setting('app.requester_id', true), '') "
        "IS NULL AS unset"
    )

    assert rows[0]["unset"] is True


@pytest.mark.asyncio
async def test_a_hostile_requester_id_cannot_inject():
    """
    Bound through set_config, never interpolated — so a requester id is a
    value, not SQL.
    """

    from app.db.connection import app_db
    from app.sql.executor import SQLExecutor

    hostile = "x'; DROP TABLE deals; --"

    rows = await SQLExecutor(database=app_db).execute(
        "SELECT current_setting('app.requester_id', true) AS who",
        requester_id=hostile,
    )

    assert rows[0]["who"] == hostile

    # The table is still there.
    assert await SQLExecutor(database=app_db).execute(
        "SELECT count(*) AS n FROM deals"
    )


@pytest.mark.asyncio
async def test_the_pool_reports_whether_it_is_actually_read_only():
    """
    Configuration says which role was requested; this says what the role can
    do. The two came apart on the development database, where marq_agent_ro
    existed as a login role with no grants at all.
    """

    from app.db.connection import app_db

    writable = not await app_db.verify_read_only()

    assert writable == (not app_db.is_read_only), (
        "the configured role and its actual privileges disagree: "
        f"is_read_only={app_db.is_read_only}, can write={writable}"
    )
