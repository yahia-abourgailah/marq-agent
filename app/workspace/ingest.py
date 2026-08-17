"""
[claude] The ingestion pipeline: bytes in, indexed file out.

    upload -> classify -> read -> register -> chunk -> embed -> index

Ordering matters in one place. The file is registered and its parsed content
persisted *before* anything is embedded, so a failure in the embedder — a
model that will not download, a Qdrant that is down — leaves a file that is
still readable and still comparable against the CRM. Only semantic search is
lost, and the warning says so.

The reverse order would mean an embedding failure discards a file the user
successfully uploaded, which is the worse of the two outcomes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.workspace.chunking import build_chunks
from app.workspace.embeddings import Embedder
from app.workspace.index import VectorIndex
from app.workspace.models import (
    IngestResult,
    ParsedFile,
    SheetSummary,
    WorkspaceFile,
)
from app.workspace.readers import read_file
from app.workspace.store import (
    WorkspaceError,
    WorkspaceStore,
    classify_extension,
    new_file_id,
    validate_slug,
)

# Upload ceiling. Large enough for a real export, small enough that one file
# cannot exhaust memory during parsing — the readers hold parsed rows in
# memory before they are written out.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


def ingest_file(
    store: WorkspaceStore,
    index: VectorIndex | None,
    embedder: Embedder | None,
    workspace_id: str,
    filename: str,
    data: bytes,
) -> IngestResult:
    """
    Read, register and index one uploaded file.

    `index` and `embedder` may be None, which ingests without semantic
    search. That combination is what makes the parsing and comparison paths
    testable with no model and no vector database present.
    """

    validate_slug(workspace_id, "workspace id")

    if not data:
        raise WorkspaceError("The uploaded file is empty.")

    if len(data) > MAX_UPLOAD_BYTES:
        raise WorkspaceError(
            f"File is {len(data) / 1_048_576:.1f} MB; the limit is "
            f"{MAX_UPLOAD_BYTES // 1_048_576} MB."
        )

    extension, kind = classify_extension(filename)

    file_id = new_file_id()

    # Write the raw bytes first — the readers work from a path, and pypdf and
    # openpyxl both want a real file rather than a buffer for streaming.
    raw_path = store.raw_path(workspace_id, file_id, extension)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(data)

    try:
        parsed, warnings = read_file(raw_path, kind, display_name=filename)
    except Exception as exc:
        raw_path.unlink(missing_ok=True)

        # [claude] Only the message, never the traceback. Parser exceptions
        # carry file paths, and this string is going into a model's context
        # and then into a user-visible answer — the same reasoning that keeps
        # driver errors out of the SQL tool's failure payloads.
        raise WorkspaceError(f"Could not read {filename!r}: {exc}") from exc

    entry = WorkspaceFile(
        file_id=file_id,
        workspace_id=workspace_id,
        filename=filename,
        kind=kind,
        byte_size=len(data),
        ingested_at=datetime.now(UTC),
        sheets=_summarise(parsed),
        page_count=len(parsed.pages),
        warnings=tuple(warnings),
    )

    replaced = _replace_previous(store, index, workspace_id, filename)

    store.save_file(entry, parsed)

    indexed, index_warnings = _index_chunks(index, embedder, entry, parsed)

    if replaced:
        index_warnings = (
            f"This replaced an earlier upload of {filename!r}.",
            *index_warnings,
        )

    if index_warnings:
        entry = replace(entry, warnings=(*entry.warnings, *index_warnings))
        store.save_file(entry, parsed)

    return IngestResult(
        file=entry,
        chunks_indexed=indexed,
        warnings=(*warnings, *index_warnings),
    )


def _replace_previous(
    store: WorkspaceStore,
    index: VectorIndex | None,
    workspace_id: str,
    filename: str,
) -> bool:
    """
    [claude] Re-uploading the same filename replaces the earlier copy.

    Found by pressure-testing: ingesting twice left two entries with the same
    name and different ids, and the agent stopped to ask which one was meant
    rather than answering. Re-uploading a corrected spreadsheet is the single
    most common thing anyone does with a reconciliation tool, so getting
    stuck there is worse than the alternative.

    Replacement rather than versioning because a workspace is a scratch area
    for the current question, not an archive — and because two files with one
    name is exactly the ambiguity that stalled the agent. The result says
    plainly that a replacement happened, so it is never silent.
    """

    previous = [
        entry
        for entry in store.list_files(workspace_id)
        if entry.filename == filename
    ]

    for entry in previous:
        store.delete_file(workspace_id, entry.file_id)

        if index is not None:
            index.delete_file(workspace_id, entry.file_id)

    return bool(previous)


def _index_chunks(
    index: VectorIndex | None,
    embedder: Embedder | None,
    entry: WorkspaceFile,
    parsed: ParsedFile,
) -> tuple[int, tuple[str, ...]]:
    """Embed and index, reporting failure rather than raising it."""

    if index is None or embedder is None:
        return 0, ()

    chunks = build_chunks(entry, parsed)

    if not chunks:
        return 0, ()

    try:
        vectors = embedder.embed_documents([chunk.text for chunk in chunks])
        return index.upsert(chunks, vectors), ()
    except Exception as exc:
        return 0, (
            "This file was stored and can be read directly, but it could "
            f"not be indexed for search ({type(exc).__name__}).",
        )


def _summarise(parsed: ParsedFile) -> tuple[SheetSummary, ...]:
    """
    Registry-level shape of any queryable rows: columns and counts, no values.

    [claude] No longer restricted to spreadsheets — a PDF carrying a ruled
    table has sheets too, and the manifest has to show them or the agent
    never learns the table is there to query.
    """

    return tuple(
        SheetSummary(
            name=sheet.name,
            columns=sheet.columns,
            row_count=sheet.row_count,
        )
        for sheet in parsed.sheets
    )


def ingest_path(
    store: WorkspaceStore,
    index: VectorIndex | None,
    embedder: Embedder | None,
    workspace_id: str,
    path: Path | str,
) -> IngestResult:
    """Ingest a file already on disk. Convenience for scripts and tests."""

    path = Path(path)

    return ingest_file(
        store=store,
        index=index,
        embedder=embedder,
        workspace_id=workspace_id,
        filename=path.name,
        data=path.read_bytes(),
    )


__all__ = ["MAX_UPLOAD_BYTES", "ingest_file", "ingest_path"]
