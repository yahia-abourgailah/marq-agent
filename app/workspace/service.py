"""
[claude] The workspace service — one object the tools talk to.

Everything below the tools (store, readers, embedder, index, exact queries)
is composed here, so `app/tools/workspace.py` stays a thin translation layer
between a model's tool call and a typed result. That is the same split the
SQL side uses, where `SQLTool` holds the wiring and the `@tool` function is
five lines.

Every method takes `workspace_id` first and passes it down. There is no
method that omits it.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.workspace.embeddings import Embedder, SentenceTransformerEmbedder
from app.workspace.index import VectorIndex, build_index
from app.workspace.ingest import ingest_file
from app.workspace.models import (
    FileKind,
    IngestResult,
    ParsedFile,
    RetrievedChunk,
    WorkspaceFile,
)
from app.workspace.query import (
    MAX_RETURNED_ROWS,
    Filter,
    QueryError,
    aggregate,
    apply_filters,
    get_sheet,
    require_column,
)
from app.workspace.store import WorkspaceStore

# How many chunks a search returns by default. Eight passages of ~700
# characters is roughly 1,400 tokens — enough coverage to answer from, small
# enough to leave the agent room to reason.
DEFAULT_SEARCH_LIMIT = 8


class WorkspaceService:
    """Uploads, retrieval, and exact reads for one deployment."""

    def __init__(
        self,
        store: WorkspaceStore,
        index: VectorIndex | None = None,
        embedder: Embedder | None = None,
        index_factory: Callable[[], VectorIndex | None] | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder

        # [claude] The index is resolved on first use, not at construction.
        #
        # Building it opens a Qdrant connection, and this service is
        # constructed while the graph is being built — so an eager index
        # made `build_supervisor_graph()` perform network I/O. The hermetic
        # test suite noticed immediately: it went from 0.88s to 3.0s and
        # started emitting connection warnings from a test that only asserts
        # the graph has a node per domain.
        #
        # Tests inject `index` directly and never touch the factory.
        self._index = index
        self._index_factory = index_factory
        self._index_resolved = index is not None

    @property
    def index(self) -> VectorIndex | None:
        """The vector index, built on first access."""

        if not self._index_resolved:
            self._index_resolved = True

            if self._index_factory is not None:
                try:
                    self._index = self._index_factory()
                except Exception:
                    # Qdrant being down costs search, not the workspace.
                    # Files still ingest, parse and reconcile.
                    self._index = None

        return self._index

    # ---------------------------------------------------------
    # Ingestion
    # ---------------------------------------------------------

    def ingest(
        self,
        workspace_id: str,
        filename: str,
        data: bytes,
    ) -> IngestResult:
        return ingest_file(
            store=self.store,
            index=self.index,
            embedder=self.embedder,
            workspace_id=workspace_id,
            filename=filename,
            data=data,
        )

    def ingest_path(self, workspace_id: str, path: Path | str) -> IngestResult:
        path = Path(path)

        return self.ingest(workspace_id, path.name, path.read_bytes())

    def delete(self, workspace_id: str, file_id: str) -> None:
        self.store.delete_file(workspace_id, file_id)

        if self.index is not None:
            self.index.delete_file(workspace_id, file_id)

    # ---------------------------------------------------------
    # Manifest
    # ---------------------------------------------------------

    def list_files(self, workspace_id: str) -> tuple[WorkspaceFile, ...]:
        return self.store.list_files(workspace_id)

    def get_file(self, workspace_id: str, file_id: str) -> WorkspaceFile:
        return self.store.get_file(workspace_id, file_id)

    def load_parsed(self, workspace_id: str, file_id: str) -> ParsedFile:
        return self.store.load_parsed(workspace_id, file_id)

    # ---------------------------------------------------------
    # Retrieval
    # ---------------------------------------------------------

    def search(
        self,
        workspace_id: str,
        question: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        file_id: str | None = None,
    ) -> tuple[list[RetrievedChunk], int]:
        """
        Semantic search across this workspace's files.

        [claude] Returns the passages that cleared the relevance floor, and
        how many were dropped for falling under it. See
        MIN_RELEVANCE_SCORE in index.py.
        """

        if self.index is None or self.embedder is None:
            raise WorkspaceUnavailable(
                "Search is not available — the workspace index is not "
                "configured."
            )

        if not question.strip():
            return [], 0

        # Confirm the file belongs to this workspace before it is used as a
        # filter, so a wrong id is an error rather than an empty result the
        # agent would report as "the file says nothing about that".
        if file_id is not None:
            self.store.get_file(workspace_id, file_id)

        vector = self.embedder.embed_query(question)

        hits = self.index.search(
            workspace_id,
            vector=vector,
            limit=limit,
            file_id=file_id,
        )

        # [claude] The relevance floor, read off the encoder rather than
        # written here.
        #
        # Nearest-neighbour search always returns neighbours. Asked about
        # staffing policy, a workspace holding a unit schedule returned
        # eight passages about units, scored around 0.1, formatted exactly
        # like passages that answer the question. The mechanism is working
        # as designed; showing its output unconditionally is the mistake.
        #
        # The number belongs to the model — see `min_relevance_score` on
        # SentenceTransformerEmbedder. An encoder that does not declare one
        # gets no floor, which is right for the token-hash fake the
        # hermetic tests use: its scores have no calibrated meaning, and
        # measured against it an unrelated query outscores a topical one.
        floor = getattr(self.embedder, "min_relevance_score", 0.0) or 0.0
        kept = [hit for hit in hits if hit.score >= floor]

        return kept, len(hits) - len(kept)

    # ---------------------------------------------------------
    # Exact reads
    # ---------------------------------------------------------

    def read_rows(
        self,
        workspace_id: str,
        file_id: str,
        sheet: str | None = None,
        filters: list[Filter] | None = None,
        columns: list[str] | None = None,
        limit: int = MAX_RETURNED_ROWS,
    ) -> dict[str, Any]:
        """Exact rows from a spreadsheet, filtered but never sampled."""

        content = self._spreadsheet(workspace_id, file_id)
        worksheet = get_sheet(content, sheet)

        rows = apply_filters(worksheet, filters or [])

        if columns:
            resolved = [require_column(worksheet, column) for column in columns]
            projected = [
                {column: row.get(column) for column in resolved} for row in rows
            ]
        else:
            projected = rows

        capped = min(max(1, limit), MAX_RETURNED_ROWS)

        return {
            "sheet": worksheet.name,
            "matched_rows": len(rows),
            "returned_rows": min(len(projected), capped),
            # [claude] Reported the same way the SQL tool reports
            # rows_available vs row_count: the agent must be able to tell a
            # complete answer from a partial one, because the difference
            # decides whether a total it computes downstream is real.
            "truncated": len(projected) > capped,
            "rows": projected[:capped],
        }

    def aggregate(
        self,
        workspace_id: str,
        file_id: str,
        operation: str,
        column: str | None = None,
        group_by: str | None = None,
        filters: list[Filter] | None = None,
        sheet: str | None = None,
    ) -> dict[str, Any]:
        """An aggregate over every matching row, not over a sample."""

        content = self._spreadsheet(workspace_id, file_id)
        worksheet = get_sheet(content, sheet)

        result = aggregate(
            worksheet,
            operation=operation,
            column=column,
            group_by=group_by,
            filters=filters,
        )

        return {"sheet": worksheet.name, **result}

    def _spreadsheet(self, workspace_id: str, file_id: str) -> ParsedFile:
        """
        The parsed content of anything with queryable rows.

        [claude] Documents qualify when a ruled table was found in them, so a
        figure in a PDF schedule can be totalled rather than only quoted. A
        document with no tables still refuses, and says why — that refusal is
        the honest answer to "what do these PDF numbers add up to" when the
        numbers are prose.
        """

        entry = self.store.get_file(workspace_id, file_id)

        parsed = self.store.load_parsed(workspace_id, file_id)

        if entry.kind is FileKind.SPREADSHEET or parsed.sheets:
            return parsed

        raise QueryError(
            f"{entry.filename!r} is a document with no tables in it, so its "
            "rows cannot be read or totalled. Use workspace_search to read "
            "what it says."
        )


class WorkspaceUnavailable(RuntimeError):
    """Raised when a capability needs infrastructure that is not configured."""


@lru_cache
def get_workspace_service() -> WorkspaceService:
    """
    The process-wide service, built from settings.

    [claude] Built on first call rather than at import. docs/HANDOFF.md
    records that `settings` and `app_db` are constructed at import time and
    that this is why the test suite needs a session-scoped event loop; the
    embedder and a Qdrant connection are heavier than either, so this one
    stays lazy.

    A Qdrant that is unreachable degrades rather than raises: files still
    ingest, still parse, and are still comparable against the CRM. Only
    semantic search is lost, and the tools say so.
    """

    from app.config import settings

    store = WorkspaceStore(root=Path(settings.workspace_root))

    embedder = SentenceTransformerEmbedder(settings.embedding_model)

    def build() -> VectorIndex:
        return build_index(
            collection=settings.workspace_collection,
            dimension=embedder.dimension,
            url=settings.qdrant_url,
            path=settings.qdrant_path,
        )

    return WorkspaceService(
        store=store,
        embedder=embedder,
        index_factory=build,
    )


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "WorkspaceService",
    "WorkspaceUnavailable",
    "get_workspace_service",
]
