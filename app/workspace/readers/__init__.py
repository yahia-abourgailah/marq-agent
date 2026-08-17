"""
[claude] File readers — one lane per kind of file.

A reader turns bytes on disk into a `ParsedFile`. It does not embed, index,
or decide anything about relevance; that is the ingestion pipeline's job.
Keeping the split means a parsing bug is reproducible without a model, an
embedder, or a vector database in the room.
"""

from __future__ import annotations

from pathlib import Path

from app.workspace.models import FileKind, ParsedFile
from app.workspace.readers.documents import read_document
from app.workspace.readers.tabular import read_spreadsheet


def read_file(
    path: Path,
    kind: FileKind,
    display_name: str | None = None,
) -> tuple[ParsedFile, tuple[str, ...]]:
    """
    Dispatch to the reader for this file's lane.

    [claude] `display_name` is the name the user uploaded. It matters for
    delimited files, which have no internal sheet name and borrow one from
    the filename — and on disk the filename is the opaque storage id. Without
    this the agent told users their data was in "sheet wf_21b07b7fe4281016".
    """

    if kind is FileKind.DOCUMENT:
        return read_document(path)

    return read_spreadsheet(path, display_name=display_name)


__all__ = ["read_document", "read_file", "read_spreadsheet"]
