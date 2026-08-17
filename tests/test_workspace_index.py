"""
[claude] Vector index behaviour, against a real in-process Qdrant.

Qdrant's local mode runs the same query and filter logic as the server, so
these are not mocks — the isolation assertions below exercise the actual
payload filter that separates one user's uploads from another's.

Retrieval *quality* is deliberately not tested here. The fake embedder is not
semantic, and asserting that a real model ranks one passage above another is
an eval, not a unit test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.workspace.chunking import build_chunks
from app.workspace.index import build_index, point_id
from app.workspace.models import (
    Chunk,
    ChunkLocator,
    ColumnSpec,
    FileKind,
    PageContent,
    ParsedFile,
    SheetContent,
    WorkspaceFile,
)
from tests.workspace_support import FakeEmbedder


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def index(embedder):
    return build_index(collection="test", dimension=embedder.dimension)


def chunk(workspace_id: str, text: str, file_id: str = "wf_1") -> Chunk:
    return Chunk(
        chunk_id=f"{file_id}:{workspace_id}:{text[:10]}",
        workspace_id=workspace_id,
        text=text,
        locator=ChunkLocator(
            file_id=file_id,
            filename="notes.pdf",
            kind=FileKind.DOCUMENT,
            page=1,
        ),
    )


def add(index, embedder, chunks):
    index.upsert(chunks, embedder.embed_documents([c.text for c in chunks]))


# ============================================================
# Round trip
# ============================================================


def test_chunks_come_back_with_their_citation(index, embedder):
    add(index, embedder, [chunk("ws1", "payment terms net thirty days")])

    hits = index.search("ws1", embedder.embed_query("payment terms"), limit=5)

    assert len(hits) == 1
    assert hits[0].chunk.text == "payment terms net thirty days"
    assert hits[0].chunk.locator.cite() == "notes.pdf, page 1"


def test_reindexing_the_same_chunk_does_not_duplicate_it(index, embedder):
    """Point ids are a uuid5 of the chunk id, so re-ingesting overwrites."""

    for _ in range(3):
        add(index, embedder, [chunk("ws1", "same text")])

    hits = index.search("ws1", embedder.embed_query("same text"), limit=10)

    assert len(hits) == 1


def test_point_ids_are_deterministic():
    assert point_id("a:b:1") == point_id("a:b:1")
    assert point_id("a:b:1") != point_id("a:b:2")


def test_upserting_nothing_is_a_no_op(index):
    assert index.upsert([], []) == 0


def test_mismatched_chunks_and_vectors_raise(index):
    with pytest.raises(ValueError):
        index.upsert([chunk("ws1", "a")], [])


# ============================================================
# Isolation — the assertions that matter
# ============================================================


def test_a_search_never_returns_another_workspaces_chunks(index, embedder):
    add(index, embedder, [chunk("ws1", "confidential alpha figures")])
    add(index, embedder, [chunk("ws2", "confidential alpha figures")])

    hits = index.search("ws1", embedder.embed_query("confidential alpha"), limit=10)

    assert len(hits) == 1
    assert all(hit.chunk.workspace_id == "ws1" for hit in hits)


def test_an_empty_workspace_returns_nothing_even_when_others_have_data(
    index, embedder
):
    add(index, embedder, [chunk("ws1", "some content")])

    assert index.search("ws3", embedder.embed_query("some content")) == []


def test_file_id_narrows_within_a_workspace(index, embedder):
    add(index, embedder, [chunk("ws1", "alpha content", file_id="wf_a")])
    add(index, embedder, [chunk("ws1", "alpha content", file_id="wf_b")])

    hits = index.search(
        "ws1", embedder.embed_query("alpha content"), file_id="wf_a", limit=10
    )

    assert [hit.chunk.locator.file_id for hit in hits] == ["wf_a"]


def test_deleting_a_file_leaves_other_files_and_workspaces_intact(
    index, embedder
):
    add(index, embedder, [chunk("ws1", "keep me", file_id="wf_keep")])
    add(index, embedder, [chunk("ws1", "drop me", file_id="wf_drop")])
    add(index, embedder, [chunk("ws2", "drop me", file_id="wf_drop")])

    index.delete_file("ws1", "wf_drop")

    # [claude] Asserted by file id rather than by an empty result. A
    # similarity search returns the nearest chunks whatever the query, so
    # "drop me" still retrieves the surviving "keep me" chunk — which is
    # precisely why the agent is told never to treat retrieval as a filter.
    remaining = index.search("ws1", embedder.embed_query("drop me"), limit=10)

    assert [hit.chunk.locator.file_id for hit in remaining] == ["wf_keep"]

    # The same file id in another workspace is untouched.
    surviving = index.search("ws2", embedder.embed_query("drop me"), limit=10)

    assert [hit.chunk.locator.file_id for hit in surviving] == ["wf_drop"]


# ============================================================
# Chunking, checked through the index
# ============================================================


def entry(kind: FileKind) -> WorkspaceFile:
    return WorkspaceFile(
        file_id="wf_1",
        workspace_id="ws1",
        filename="book.xlsx" if kind is FileKind.SPREADSHEET else "doc.pdf",
        kind=kind,
        byte_size=1,
        ingested_at=datetime.now(UTC),
    )


def test_sheet_chunks_carry_a_row_range_citation():
    sheet = SheetContent(
        name="Deals",
        columns=(ColumnSpec("id", "number", 45),),
        rows=tuple({"id": i} for i in range(1, 46)),
    )

    chunks = build_chunks(
        entry(FileKind.SPREADSHEET),
        ParsedFile(kind=FileKind.SPREADSHEET, sheets=(sheet,)),
    )

    # 45 rows at 20 per chunk.
    assert len(chunks) == 3

    assert chunks[0].locator.cite() == "book.xlsx [Deals] rows 1-20"
    assert chunks[2].locator.cite() == "book.xlsx [Deals] rows 41-45"


def test_sheet_chunks_repeat_column_names_so_values_carry_meaning():
    """
    A bare value list embeds almost no signal about what those values are.
    """

    sheet = SheetContent(
        name="Deals",
        columns=(ColumnSpec("amount", "number", 1),),
        rows=({"amount": 12000},),
    )

    chunks = build_chunks(
        entry(FileKind.SPREADSHEET),
        ParsedFile(kind=FileKind.SPREADSHEET, sheets=(sheet,)),
    )

    assert "amount: 12000" in chunks[0].text


def test_document_chunks_carry_their_page_number():
    parsed = ParsedFile(
        kind=FileKind.DOCUMENT,
        pages=(PageContent(1, "first page"), PageContent(2, "second page")),
    )

    chunks = build_chunks(entry(FileKind.DOCUMENT), parsed)

    assert [c.locator.page for c in chunks] == [1, 2]
    assert chunks[1].locator.cite() == "doc.pdf, page 2"


def test_empty_pages_produce_no_chunks():
    parsed = ParsedFile(
        kind=FileKind.DOCUMENT,
        pages=(PageContent(1, ""), PageContent(2, "real text")),
    )

    chunks = build_chunks(entry(FileKind.DOCUMENT), parsed)

    assert len(chunks) == 1
    assert chunks[0].locator.page == 2


def test_a_long_page_splits_into_several_chunks_all_citing_that_page():
    paragraph = "word " * 400

    parsed = ParsedFile(
        kind=FileKind.DOCUMENT,
        pages=(PageContent(7, paragraph),),
    )

    chunks = build_chunks(entry(FileKind.DOCUMENT), parsed)

    assert len(chunks) > 1
    assert {c.locator.page for c in chunks} == {7}
