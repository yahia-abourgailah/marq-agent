"""
[claude] The vector index — Qdrant, scoped hard to one workspace.

Isolation is enforced by shape, not discipline
----------------------------------------------
`workspace_id` is a required positional argument on every read, and the
filter built from it is added by this module rather than by the caller. There
is no code path that searches without it, so a tool cannot leak one user's
uploads into another's answer by forgetting a `filter=` keyword. Deleting a
file is scoped the same way.

Storage
-------
One collection holds every workspace, partitioned by an indexed payload
field. The alternative — a collection per workspace — makes isolation
obvious but multiplies collections without bound and makes them expensive to
enumerate. A payload filter on an indexed keyword field is the shape Qdrant
is built for.

Vectors are cosine-normalised by the embedder, so the collection uses cosine
distance and the scores it returns are directly comparable across queries.
"""

from __future__ import annotations

import uuid
import warnings
from functools import cache
from typing import TYPE_CHECKING, Any

from app.workspace.models import Chunk, ChunkLocator, FileKind, RetrievedChunk

if TYPE_CHECKING:
    from qdrant_client import QdrantClient


@cache
def _qmodels() -> Any:
    """
    The Qdrant request models, imported on first use.

    [claude] Importing qdrant_client at module load cost ~320ms and showed up
    directly in the hermetic test suite, which this module is not otherwise
    part of: `app.tools.workspace` reaches here through the service, so
    merely building the graph paid for a vector-database client.

    Deferring it has a second benefit worth more than the milliseconds — the
    parse, read and compare paths no longer require qdrant-client to be
    installed at all. A deployment that never turns on search does not need
    the dependency present to use its uploaded files.
    """

    from qdrant_client.http import models

    return models

# [claude] Namespace for deterministic point ids. Qdrant accepts only an
# unsigned integer or a UUID as a point id, and chunk ids are neither. A
# uuid5 over the chunk id keeps them deterministic, so re-ingesting the same
# file overwrites its points instead of duplicating them.
POINT_NAMESPACE = uuid.UUID("6f0f0f2a-6d51-5c8c-9f3a-1f9a2b0d7e11")


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, chunk_id))


class IndexModelMismatch(RuntimeError):
    """The collection holds vectors from a different encoder."""


class VectorIndex:
    """Chunk storage and similarity search for uploaded files."""

    def __init__(
        self,
        client: QdrantClient,
        collection: str,
        dimension: int,
    ) -> None:
        self.client = client
        self.collection = collection
        self.dimension = dimension

    # ---------------------------------------------------------
    # Collection
    # ---------------------------------------------------------

    def ensure_collection(self) -> None:
        """
        Create the collection and its payload index if absent.

        The index on `workspace_id` is not optional. Without it Qdrant still
        filters correctly but scans to do so, and the filter is on the hot
        path of every single search.
        """

        qmodels = _qmodels()

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=qmodels.VectorParams(
                    size=self.dimension,
                    distance=qmodels.Distance.COSINE,
                ),
            )

        for field in ("workspace_id", "file_id"):
            try:
                # [claude] Local mode warns that payload indexes do nothing
                # there, which is true and not actionable: filtering is still
                # correct, and the index only matters against a server. The
                # warning is suppressed so it does not become noise in every
                # test run that touches the workspace.
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore", message=".*[Pp]ayload indexes.*"
                    )

                    self.client.create_payload_index(
                        collection_name=self.collection,
                        field_name=field,
                        field_schema=qmodels.PayloadSchemaType.KEYWORD,
                    )
            except Exception:
                # Already indexed. Qdrant has no idempotent form of this
                # call, and the alternative is reading the collection info
                # on every startup to discover what we already know.
                pass

    def stored_embedding_model(self) -> str | None:
        """
        The model that built the vectors already in this collection.

        [claude] Read from a single point rather than tracked separately,
        so it cannot disagree with the vectors it describes. Returns None
        for an empty collection, and for one written before this was
        recorded — neither is a mismatch, and treating "unknown" as "wrong"
        would refuse every existing index once.
        """

        try:
            points, _ = self.client.scroll(
                collection_name=self.collection,
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return None

        if not points:
            return None

        return (points[0].payload or {}).get("embedding_model")

    def check_embedding_model(self, embedding_model: str | None) -> None:
        """
        Refuse to add vectors from a different model than the collection holds.

        [claude] A dimension change already fails loudly: the collection is
        sized from the model and Qdrant rejects the mismatch. A change to a
        *same-dimension* model is the silent one — the old and new vectors
        share a collection while occupying different embedding spaces, so
        every distance between them is meaningless and search degrades for
        the older files with no error anywhere.

        Raising here means a model swap surfaces as "this file could not be
        indexed", which is how ingestion already reports a dead index, and
        the file is still stored and readable. That is a better failure than
        a silently worse search — and a much better one than refusing to
        start, which would take the CRM agent down over a workspace feature
        that is designed to degrade.
        """

        if embedding_model is None:
            return

        stored = self.stored_embedding_model()

        if stored is not None and stored != embedding_model:
            raise IndexModelMismatch(
                f"this index was built with {stored!r} but the configured "
                f"encoder is {embedding_model!r}; vectors from two models "
                "cannot be compared. Re-index the workspace, or restore the "
                "previous encoder."
            )

    # ---------------------------------------------------------
    # Writing
    # ---------------------------------------------------------

    def upsert(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]],
        embedding_model: str | None = None,
    ) -> int:
        """
        Index chunks with their vectors. Returns the number written.

        [claude] `embedding_model` is stamped on every point.

        A model swap that changes the dimension already fails loudly —
        `ensure_collection` sizes the collection from it and Qdrant rejects
        the mismatch. A swap to a *same-dimension* model does not: the old
        and new vectors coexist in one collection occupying different
        embedding spaces, every distance between them is meaningless, and
        search quietly degrades for the older files with no error anywhere.

        Recording it per point is what makes that detectable after the
        fact, and lets a re-index target only the stale vectors.
        """

        qmodels = _qmodels()

        if not chunks:
            return 0

        if len(chunks) != len(vectors):
            raise ValueError(
                f"Got {len(chunks)} chunks and {len(vectors)} vectors."
            )

        points = [
            qmodels.PointStruct(
                id=point_id(chunk.chunk_id),
                vector=vector,
                payload={
                    "chunk_id": chunk.chunk_id,
                    "embedding_model": embedding_model,
                    "workspace_id": chunk.workspace_id,
                    "text": chunk.text,
                    "file_id": chunk.locator.file_id,
                    "filename": chunk.locator.filename,
                    "kind": chunk.locator.kind.value,
                    "page": chunk.locator.page,
                    "sheet": chunk.locator.sheet,
                    "row_start": chunk.locator.row_start,
                    "row_end": chunk.locator.row_end,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

        self.client.upsert(collection_name=self.collection, points=points)

        return len(points)

    # ---------------------------------------------------------
    # Reading
    # ---------------------------------------------------------

    def search(
        self,
        workspace_id: str,
        vector: list[float],
        limit: int = 8,
        file_id: str | None = None,
        min_score: float = 0.0,
    ) -> list[RetrievedChunk]:
        """
        Nearest chunks within one workspace.

        `workspace_id` is positional and always applied. `file_id` narrows
        further, for "what does *this* file say" questions.

        [claude] `min_score` is a capability offered here and a policy set
        one layer up: WorkspaceService applies the calibrated floor and
        reports what it dropped. Storage should be able to answer "nearest
        eight"; deciding that the nearest eight are not good enough to show
        anybody is a retrieval-quality judgement, and it belongs with the
        rest of them.
        """

        qmodels = _qmodels()

        conditions = [
            qmodels.FieldCondition(
                key="workspace_id",
                match=qmodels.MatchValue(value=workspace_id),
            )
        ]

        if file_id is not None:
            conditions.append(
                qmodels.FieldCondition(
                    key="file_id",
                    match=qmodels.MatchValue(value=file_id),
                )
            )

        response = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=qmodels.Filter(must=conditions),
            limit=limit,
            with_payload=True,
        )

        results: list[RetrievedChunk] = []

        for point in response.points:
            if point.score < min_score:
                continue

            payload = point.payload or {}

            # Defence in depth. The filter above is the enforcement point;
            # this asserts it held, because a bug that returns another
            # workspace's content is one that must never fail quietly.
            if payload.get("workspace_id") != workspace_id:
                continue

            results.append(
                RetrievedChunk(
                    chunk=Chunk(
                        chunk_id=payload.get("chunk_id", ""),
                        workspace_id=workspace_id,
                        text=payload.get("text", ""),
                        locator=ChunkLocator(
                            file_id=payload.get("file_id", ""),
                            filename=payload.get("filename", ""),
                            kind=FileKind(payload.get("kind", "document")),
                            page=payload.get("page"),
                            sheet=payload.get("sheet"),
                            row_start=payload.get("row_start"),
                            row_end=payload.get("row_end"),
                        ),
                    ),
                    score=float(point.score),
                )
            )

        return results

    def delete_file(self, workspace_id: str, file_id: str) -> None:
        """Drop every chunk of one file. Scoped, like every other read."""

        qmodels = _qmodels()

        self.client.delete(
            collection_name=self.collection,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="workspace_id",
                            match=qmodels.MatchValue(value=workspace_id),
                        ),
                        qmodels.FieldCondition(
                            key="file_id",
                            match=qmodels.MatchValue(value=file_id),
                        ),
                    ]
                )
            ),
        )


def build_index(
    collection: str,
    dimension: int,
    url: str | None = None,
    path: str | None = None,
) -> VectorIndex:
    """
    Build an index against a Qdrant server, or an in-process one.

    Three modes, in precedence order:

        url    a Qdrant server. What production uses.
        path   Qdrant's engine embedded in this process, persisted to disk.
        (none) the same engine, in memory only.

    [claude] `path` was added because local development has no Qdrant server:
    there is no Homebrew formula, and Docker is not always present. Embedded
    mode runs the real filter and query logic — the isolation guarantees hold
    identically — so it is a faithful way to develop and demo against.

    It is not a production option. Embedded mode is single-process, takes an
    exclusive lock on its directory, and ignores payload indexes, so filtering
    is correct but scans. Point `QDRANT_URL` at a server before this carries
    real traffic.

    In-memory is what the hermetic tests use, and is why the whole retrieval
    path is testable with no infrastructure at all.
    """

    from qdrant_client import QdrantClient

    if url:
        client = QdrantClient(url=url)
    elif path:
        client = QdrantClient(path=path)
    else:
        client = QdrantClient(location=":memory:")

    index = VectorIndex(client=client, collection=collection, dimension=dimension)
    index.ensure_collection()

    return index


__all__ = ["POINT_NAMESPACE", "VectorIndex", "build_index", "point_id"]
