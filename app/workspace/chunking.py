"""
[claude] Turning parsed content into embeddable chunks.

Chunking is where the two lanes stop looking alike. A document is prose, so
it splits on paragraph boundaries within a page. A spreadsheet is records, so
it splits into row blocks — and each block repeats its column names, because
an embedded fragment reading `12000 | contracted | 2026-03-04` matches
nothing a person would type, while `amount: 12000 | status: contracted`
matches "which contracted rows are worth twelve thousand".

Both carry a locator, so every retrieved passage can say where it came from.

What a chunk is *not*
---------------------
It is not the source of truth for any number. Retrieval returns the passages
most similar to a question, which is not the same set as the rows matching a
condition. Aggregates come from the parsed content — see models.py.
"""

from __future__ import annotations

from app.workspace.models import (
    Chunk,
    ChunkLocator,
    FileKind,
    ParsedFile,
    SheetContent,
    WorkspaceFile,
)

# Target size for a document chunk, in characters. Roughly 200 tokens, which
# sits well inside the 128-token window of the MiniLM sentence encoders —
# text past that window is silently ignored by the model, so oversized chunks
# embed only their opening and retrieve badly.
DOCUMENT_CHUNK_CHARS = 700
DOCUMENT_OVERLAP_CHARS = 100

# Rows per spreadsheet chunk. Small enough that a hit points at a tight row
# range the user can actually look at.
ROWS_PER_CHUNK = 20


def build_chunks(entry: WorkspaceFile, parsed: ParsedFile) -> list[Chunk]:
    """Produce every chunk for one parsed file."""

    if parsed.kind is FileKind.DOCUMENT:
        return _document_chunks(entry, parsed)

    return _sheet_chunks(entry, parsed)


# ============================================================
# Documents
# ============================================================


def _document_chunks(entry: WorkspaceFile, parsed: ParsedFile) -> list[Chunk]:
    chunks: list[Chunk] = []

    for page in parsed.pages:
        if not page.text.strip():
            continue

        for position, segment in enumerate(_split_text(page.text)):
            locator = ChunkLocator(
                file_id=entry.file_id,
                filename=entry.filename,
                kind=FileKind.DOCUMENT,
                page=page.number,
            )

            chunks.append(
                Chunk(
                    chunk_id=f"{entry.file_id}:p{page.number}:{position}",
                    workspace_id=entry.workspace_id,
                    text=segment,
                    locator=locator,
                )
            )

    return chunks


def _split_text(text: str) -> list[str]:
    """
    Split a page into overlapping segments on paragraph boundaries.

    [claude] Overlap exists so a sentence straddling a boundary is fully
    present in one of the two segments. Without it, the single most
    quotable line on a page can end up half in each and retrieve as neither.
    """

    text = text.strip()

    if len(text) <= DOCUMENT_CHUNK_CHARS:
        return [text]

    paragraphs = [
        paragraph.strip()
        for paragraph in text.split("\n\n")
        if paragraph.strip()
    ]

    if not paragraphs:
        paragraphs = [text]

    segments: list[str] = []
    current = ""

    for paragraph in paragraphs:
        # A single paragraph longer than the target is hard-split; there is
        # no smaller natural boundary to use.
        while len(paragraph) > DOCUMENT_CHUNK_CHARS:
            if current:
                segments.append(current)
                current = ""

            segments.append(paragraph[:DOCUMENT_CHUNK_CHARS])
            paragraph = paragraph[
                DOCUMENT_CHUNK_CHARS - DOCUMENT_OVERLAP_CHARS :
            ]

        if not current:
            current = paragraph
        elif len(current) + len(paragraph) + 2 <= DOCUMENT_CHUNK_CHARS:
            current = f"{current}\n\n{paragraph}"
        else:
            segments.append(current)
            current = paragraph

    if current:
        segments.append(current)

    return segments


# ============================================================
# Spreadsheets
# ============================================================


def _sheet_chunks(entry: WorkspaceFile, parsed: ParsedFile) -> list[Chunk]:
    chunks: list[Chunk] = []

    for sheet in parsed.sheets:
        for start in range(0, sheet.row_count, ROWS_PER_CHUNK):
            block = sheet.rows[start : start + ROWS_PER_CHUNK]

            if not block:
                continue

            # Row numbers are 1-based and exclude the header, so they match
            # what someone counting data rows would say.
            row_start = start + 1
            row_end = start + len(block)

            locator = ChunkLocator(
                file_id=entry.file_id,
                filename=entry.filename,
                kind=FileKind.SPREADSHEET,
                sheet=sheet.name,
                row_start=row_start,
                row_end=row_end,
            )

            chunks.append(
                Chunk(
                    chunk_id=f"{entry.file_id}:{sheet.name}:{row_start}",
                    workspace_id=entry.workspace_id,
                    text=_render_rows(sheet, block),
                    locator=locator,
                )
            )

    return chunks


def _render_rows(sheet: SheetContent, rows: tuple[dict, ...]) -> str:
    """
    Render a block of rows as labelled text.

    Column names are repeated per row rather than written once as a header.
    It is more verbose, and it is what makes a row retrievable on its own
    terms — the embedding of a bare value list carries almost no signal
    about what those values mean.
    """

    lines = [f"Sheet: {sheet.name}"]

    for row in rows:
        parts = [
            f"{column}: {row[column]}"
            for column in sheet.column_names
            if row.get(column) not in (None, "")
        ]

        if parts:
            lines.append(" | ".join(parts))

    return "\n".join(lines)


__all__ = [
    "DOCUMENT_CHUNK_CHARS",
    "DOCUMENT_OVERLAP_CHARS",
    "ROWS_PER_CHUNK",
    "build_chunks",
]
