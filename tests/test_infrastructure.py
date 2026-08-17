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
