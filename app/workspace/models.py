"""
[claude] Value types for uploaded files, their parsed content, and chunks.

Why the parsed content is modelled at all
-----------------------------------------
It would be simpler to read a file, embed it, and keep only the vectors. That
is enough to answer "what does this document say", and not enough to answer
"does this sheet agree with the CRM".

Retrieval returns the passages most similar to a question. It does not return
*all* the rows that match a condition, and it carries no guarantee that the
top-k passages contain every relevant number. Summing what came back is
therefore a sampling error dressed up as an answer — the same shape as the
denominator bugs already documented in docs/HANDOFF.md, where a confident
wrong number is produced instead of an error.

So a reader keeps two things: the chunks that get embedded, and the parsed
content that stays exact. `SheetContent` is the second one. Aggregates and
comparisons run over it, so they are computed rather than recalled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class FileKind(StrEnum):
    """
    How a file is read.

    The two lanes are deliberately asymmetric. A spreadsheet is data with a
    schema; a document is prose with a layout. They are chunked differently,
    cited differently, and only one of them is queryable for exact values.
    """

    SPREADSHEET = "spreadsheet"
    DOCUMENT = "document"


# ============================================================
# Parsed content
# ============================================================


@dataclass(frozen=True)
class ColumnSpec:
    """
    One spreadsheet column as the agent is allowed to see it.

    [claude] `dtype` is inferred, not declared — spreadsheets have no schema,
    and the header row is the only naming we get. It is coarse on purpose:
    "number", "date" or "text" is enough for the agent to know whether a
    column can be summed, and anything finer would invite it to trust a
    guess.

    Deliberately carries no sample values. The agent gets the shape of the
    file from the manifest and the values from an explicit read, so uploaded
    customer data never rides along in a prompt just because a file exists.
    """

    name: str
    dtype: str
    non_null: int


@dataclass(frozen=True)
class SheetContent:
    """One worksheet: a header, typed columns, and every parsed row."""

    name: str
    columns: tuple[ColumnSpec, ...]

    # Row values keyed by column name. Kept in file order, because a row
    # number is how a spreadsheet answer is cited.
    rows: tuple[dict[str, object], ...] = ()

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class PageContent:
    """One page of a document. `number` is 1-based, as printed."""

    number: int
    text: str


@dataclass(frozen=True)
class ParsedFile:
    """
    Everything a reader produced for one file.

    Exactly one of `sheets` / `pages` is populated, decided by `kind`.
    """

    kind: FileKind
    sheets: tuple[SheetContent, ...] = ()
    pages: tuple[PageContent, ...] = ()


# ============================================================
# Registry
# ============================================================


@dataclass(frozen=True)
class SheetSummary:
    """The shape of a worksheet, for the manifest. No values."""

    name: str
    columns: tuple[ColumnSpec, ...]
    row_count: int


@dataclass(frozen=True)
class WorkspaceFile:
    """
    A file's registry entry — what it is, not what it contains.

    This is what `list_workspace_files` returns and what the agent reasons
    about before deciding to read anything.
    """

    file_id: str
    workspace_id: str
    filename: str
    kind: FileKind
    byte_size: int
    ingested_at: datetime

    sheets: tuple[SheetSummary, ...] = ()
    page_count: int = 0

    # Set when the reader recovered content but not all of it — an
    # unreadable sheet, a scanned page with no text layer. Surfaced to the
    # agent so a thin answer can be explained rather than silently given.
    warnings: tuple[str, ...] = ()


# ============================================================
# Chunks and retrieval
# ============================================================


@dataclass(frozen=True)
class ChunkLocator:
    """
    Where a chunk came from, so an answer can cite it.

    [claude] Citations are not decoration here. The agent is answering from
    a file the user has in front of them, and "page 4" or "rows 120-139 of
    Sheet1" is what makes the answer checkable — the same reason the SQL
    side reports `rows_available` rather than just handing over rows.
    """

    file_id: str
    filename: str
    kind: FileKind

    page: int | None = None
    sheet: str | None = None
    row_start: int | None = None
    row_end: int | None = None

    def cite(self) -> str:
        """Human-readable source reference."""

        if self.kind is FileKind.DOCUMENT:
            return f"{self.filename}, page {self.page}"

        if self.row_start == self.row_end:
            return f"{self.filename} [{self.sheet}] row {self.row_start}"

        return (
            f"{self.filename} [{self.sheet}] "
            f"rows {self.row_start}-{self.row_end}"
        )


@dataclass(frozen=True)
class Chunk:
    """One embeddable unit of file content."""

    chunk_id: str
    workspace_id: str
    text: str
    locator: ChunkLocator


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk returned by a search, with its similarity score."""

    chunk: Chunk
    score: float


@dataclass(frozen=True)
class IngestResult:
    """Outcome of ingesting one file."""

    file: WorkspaceFile
    chunks_indexed: int
    warnings: tuple[str, ...] = field(default=())


__all__ = [
    "Chunk",
    "ChunkLocator",
    "ColumnSpec",
    "FileKind",
    "IngestResult",
    "PageContent",
    "ParsedFile",
    "RetrievedChunk",
    "SheetContent",
    "SheetSummary",
    "WorkspaceFile",
]
