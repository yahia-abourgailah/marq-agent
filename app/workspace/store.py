"""
[claude] Per-workspace file storage and registry.

Isolation is the whole job
--------------------------
Every read this module performs is scoped by `workspace_id`, and the scoping
is structural rather than conventional: a workspace is a directory, and the
store never looks outside the one it was asked for. There is no method that
returns files across workspaces, so a tool cannot accidentally leak one
user's upload into another's answer by forgetting a filter.

Two rules keep that true when the identifiers come from outside:

*   `workspace_id` and `file_id` are validated against a strict slug pattern
    before they are ever joined onto a path. `../` never reaches the
    filesystem.

*   The uploaded filename is **recorded but never used as a path**. Files are
    stored as `<file_id><extension>`, with the extension taken from a fixed
    allowlist. A file called `../../.env` is stored as
    `wf_abc123.txt` and displayed by its original name.

Layout:

    <root>/<workspace_id>/files.json          registry
    <root>/<workspace_id>/raw/<file_id><ext>  original upload
    <root>/<workspace_id>/parsed/<id>.json    reader output, exact values
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

from app.workspace.models import (
    ColumnSpec,
    FileKind,
    PageContent,
    ParsedFile,
    SheetContent,
    SheetSummary,
    WorkspaceFile,
)


class WorkspaceError(ValueError):
    """Raised when a workspace request is malformed or refers to nothing."""


# [claude] Identifiers become path segments, so they are constrained rather
# than escaped. Escaping is a thing you can get subtly wrong; a slug
# allowlist is a thing you cannot.
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Extensions the readers understand, mapped to their lane.
SUPPORTED_EXTENSIONS: dict[str, FileKind] = {
    ".pdf": FileKind.DOCUMENT,
    ".xlsx": FileKind.SPREADSHEET,
    ".xlsm": FileKind.SPREADSHEET,
    ".csv": FileKind.SPREADSHEET,
    ".tsv": FileKind.SPREADSHEET,
}


def validate_slug(value: str, label: str) -> str:
    """Reject anything that could escape its directory."""

    if not value or not SLUG.match(value):
        raise WorkspaceError(
            f"Invalid {label} {value!r}: expected letters, digits, "
            "hyphen or underscore, up to 64 characters."
        )

    return value


def classify_extension(filename: str) -> tuple[str, FileKind]:
    """
    Map a filename onto a storage extension and a reading lane.

    Raises WorkspaceError for anything the readers do not handle, so an
    unsupported upload fails at ingestion rather than producing an empty
    file the agent would later describe as "containing nothing".
    """

    extension = Path(filename).suffix.lower()

    if extension not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise WorkspaceError(
            f"Unsupported file type {extension or '(none)'!r}. "
            f"Supported: {supported}."
        )

    return extension, SUPPORTED_EXTENSIONS[extension]


def new_file_id() -> str:
    """Opaque, unguessable, and a valid path segment."""

    return f"wf_{secrets.token_hex(8)}"


class WorkspaceStore:
    """Files and parsed content on local disk, isolated per workspace."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # ---------------------------------------------------------
    # Paths
    # ---------------------------------------------------------

    def _workspace_dir(self, workspace_id: str) -> Path:
        validate_slug(workspace_id, "workspace id")
        return self.root / workspace_id

    def _registry_path(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "files.json"

    def _parsed_path(self, workspace_id: str, file_id: str) -> Path:
        validate_slug(file_id, "file id")
        return self._workspace_dir(workspace_id) / "parsed" / f"{file_id}.json"

    def raw_path(self, workspace_id: str, file_id: str, extension: str) -> Path:
        validate_slug(file_id, "file id")
        return self._workspace_dir(workspace_id) / "raw" / f"{file_id}{extension}"

    # ---------------------------------------------------------
    # Registry
    # ---------------------------------------------------------

    def list_files(self, workspace_id: str) -> tuple[WorkspaceFile, ...]:
        """Every file in this workspace, oldest first. Never crosses workspaces."""

        path = self._registry_path(workspace_id)

        if not path.is_file():
            return ()

        raw = json.loads(path.read_text(encoding="utf-8"))

        return tuple(_decode_file(entry) for entry in raw.get("files", []))

    def get_file(self, workspace_id: str, file_id: str) -> WorkspaceFile:
        """One file, or WorkspaceError. Never returns another workspace's file."""

        validate_slug(file_id, "file id")

        for entry in self.list_files(workspace_id):
            if entry.file_id == file_id:
                return entry

        raise WorkspaceError(
            f"No file {file_id!r} in this workspace."
        )

    def save_file(
        self,
        entry: WorkspaceFile,
        parsed: ParsedFile,
        source: Path | bytes | None = None,
    ) -> WorkspaceFile:
        """
        Register a file and persist its parsed content.

        [claude] The registry is rewritten whole rather than appended to.
        The alternative — appending a line per file — is faster and gives a
        half-written registry if the process dies mid-write. A workspace
        holds a handful of files, so the simple thing is also the correct
        thing here.
        """

        workspace_dir = self._workspace_dir(entry.workspace_id)
        (workspace_dir / "parsed").mkdir(parents=True, exist_ok=True)

        if source is not None:
            extension, _ = classify_extension(entry.filename)
            destination = self.raw_path(
                entry.workspace_id, entry.file_id, extension
            )
            destination.parent.mkdir(parents=True, exist_ok=True)

            if isinstance(source, bytes):
                destination.write_bytes(source)
            else:
                shutil.copyfile(source, destination)

        self._parsed_path(entry.workspace_id, entry.file_id).write_text(
            json.dumps(_encode_parsed(parsed), ensure_ascii=False),
            encoding="utf-8",
        )

        # Read and write the registry under one lock, or a simultaneous
        # upload reads the same "before" list and overwrites this entry.
        with self._registry_lock(entry.workspace_id):
            existing = [
                file
                for file in self.list_files(entry.workspace_id)
                if file.file_id != entry.file_id
            ]

            self._write_registry(entry.workspace_id, [*existing, entry])

        return entry

    def load_parsed(self, workspace_id: str, file_id: str) -> ParsedFile:
        """
        The reader's exact output for one file.

        This is what makes a spreadsheet answer computable rather than
        recalled — see the module docstring in models.py.
        """

        # Resolve through the registry first, so an id belonging to another
        # workspace raises rather than reading a path that happens to exist.
        self.get_file(workspace_id, file_id)

        path = self._parsed_path(workspace_id, file_id)

        if not path.is_file():
            raise WorkspaceError(
                f"Parsed content for {file_id!r} is missing."
            )

        return _decode_parsed(json.loads(path.read_text(encoding="utf-8")))

    def delete_file(self, workspace_id: str, file_id: str) -> None:
        """Remove a file, its parsed content and its registry entry."""

        entry = self.get_file(workspace_id, file_id)

        self._parsed_path(workspace_id, file_id).unlink(missing_ok=True)

        extension, _ = classify_extension(entry.filename)
        self.raw_path(workspace_id, file_id, extension).unlink(missing_ok=True)

        with self._registry_lock(workspace_id):
            remaining = [
                file
                for file in self.list_files(workspace_id)
                if file.file_id != file_id
            ]

            self._write_registry(workspace_id, remaining)

    @contextmanager
    def _registry_lock(self, workspace_id: str):
        """
        [claude] Hold an exclusive lock while the registry is rewritten.

        Found by a concurrency test: the registry is a read-modify-write of
        one JSON file, so two simultaneous uploads each read the old list and
        write back their own version — and one upload vanishes. The user is
        told the file was accepted and it is simply not there afterwards.

        Nothing is concurrent today because there is no HTTP layer. An
        upload endpoint is the next thing planned, which is exactly why this
        was worth fixing before it could ever happen for real.

        flock is POSIX; on a platform without it the lock degrades to a
        no-op and the original race returns. That is acceptable here — the
        target is Linux — but it is the reason this is a documented lock and
        not an assumed one.
        """

        path = self._workspace_dir(workspace_id) / ".registry.lock"
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
            except (OSError, AttributeError, NameError):
                yield
                return

            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _write_registry(
        self,
        workspace_id: str,
        files: list[WorkspaceFile],
    ) -> None:
        """
        Replace the registry atomically.

        [claude] Written to a sibling file and moved into place, because
        `write_text` truncates before it writes: a reader arriving in that
        window got an empty file and a JSONDecodeError in the middle of
        answering a question. os.replace is atomic on POSIX, so a reader now
        sees either the old registry or the new one and never a partial one.
        """

        path = self._registry_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(
            {"files": [_encode_file(file) for file in files]},
            ensure_ascii=False,
            indent=2,
        )

        # Same directory, so the move stays on one filesystem and is atomic.
        handle, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=".registry-", suffix=".tmp"
        )

        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())

            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


# ============================================================
# Serialisation
# ============================================================
#
# [claude] Written out explicitly rather than through dataclasses.asdict().
# asdict() round-trips neither the FileKind enum nor the datetime, and the
# failure mode is a registry that loads with strings where the code expects
# objects — which shows up much later, as an attribute error inside a tool.


def _encode_column(column: ColumnSpec) -> dict:
    return {
        "name": column.name,
        "dtype": column.dtype,
        "non_null": column.non_null,
    }


def _decode_column(raw: dict) -> ColumnSpec:
    return ColumnSpec(
        name=raw["name"],
        dtype=raw["dtype"],
        non_null=raw["non_null"],
    )


def _encode_file(entry: WorkspaceFile) -> dict:
    return {
        "file_id": entry.file_id,
        "workspace_id": entry.workspace_id,
        "filename": entry.filename,
        "kind": entry.kind.value,
        "byte_size": entry.byte_size,
        "ingested_at": entry.ingested_at.isoformat(),
        "page_count": entry.page_count,
        "warnings": list(entry.warnings),
        "sheets": [
            {
                "name": sheet.name,
                "row_count": sheet.row_count,
                "columns": [_encode_column(column) for column in sheet.columns],
            }
            for sheet in entry.sheets
        ],
    }


def _decode_file(raw: dict) -> WorkspaceFile:
    return WorkspaceFile(
        file_id=raw["file_id"],
        workspace_id=raw["workspace_id"],
        filename=raw["filename"],
        kind=FileKind(raw["kind"]),
        byte_size=raw["byte_size"],
        ingested_at=_parse_timestamp(raw["ingested_at"]),
        page_count=raw.get("page_count", 0),
        warnings=tuple(raw.get("warnings", ())),
        sheets=tuple(
            SheetSummary(
                name=sheet["name"],
                row_count=sheet["row_count"],
                columns=tuple(
                    _decode_column(column) for column in sheet["columns"]
                ),
            )
            for sheet in raw.get("sheets", [])
        ),
    )


def _encode_parsed(parsed: ParsedFile) -> dict:
    return {
        "kind": parsed.kind.value,
        "sheets": [
            {
                "name": sheet.name,
                "columns": [_encode_column(column) for column in sheet.columns],
                "rows": list(sheet.rows),
            }
            for sheet in parsed.sheets
        ],
        "pages": [
            {"number": page.number, "text": page.text} for page in parsed.pages
        ],
    }


def _decode_parsed(raw: dict) -> ParsedFile:
    return ParsedFile(
        kind=FileKind(raw["kind"]),
        sheets=tuple(
            SheetContent(
                name=sheet["name"],
                columns=tuple(
                    _decode_column(column) for column in sheet["columns"]
                ),
                rows=tuple(sheet["rows"]),
            )
            for sheet in raw.get("sheets", [])
        ),
        pages=tuple(
            PageContent(number=page["number"], text=page["text"])
            for page in raw.get("pages", [])
        ),
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    return parsed


__all__ = [
    "SLUG",
    "SUPPORTED_EXTENSIONS",
    "WorkspaceError",
    "WorkspaceStore",
    "classify_extension",
    "new_file_id",
    "validate_slug",
]
