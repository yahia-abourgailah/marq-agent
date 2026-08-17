"""
[claude] The embedding model, behind a protocol and loaded lazily.

Why lazy, and why a protocol
----------------------------
`sentence-transformers` imports torch, which costs seconds and hundreds of
megabytes of resident memory. docs/HANDOFF.md already records the cost of
building things at import time: `settings` and `app_db` are constructed when
their modules load, which is why the test suite needs a session-scoped event
loop and why the API layer will need a lifespan to undo it.

So this one does not repeat that. The module imports cleanly with torch
absent from the process; the model is constructed on first `embed` and cached
thereafter. `pytest` stays hermetic and sub-second because the tests inject a
fake through the `Embedder` protocol instead.

The model
---------
`paraphrase-multilingual-MiniLM-L12-v2` — 384 dimensions, and multilingual,
which is the reason to prefer it here: a CRM covering franchises, projects
and developers carries names and notes in more than one language, and an
English-only encoder puts those in the wrong part of the space.

Its input window is 128 word-pieces. Text beyond that is truncated by the
model itself, without complaint, so chunk sizes in chunking.py are set to
stay inside it — an oversized chunk does not error, it just embeds its
opening and retrieves badly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_EMBEDDING_DIMENSION = 384


@runtime_checkable
class Embedder(Protocol):
    """What the ingestion pipeline and the search tool need from a model."""

    @property
    def dimension(self) -> int:
        """Vector width, needed to create the Qdrant collection."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed chunk text for indexing."""

    def embed_query(self, text: str) -> list[float]:
        """Embed one search query."""


class SentenceTransformerEmbedder:
    """
    Local sentence-transformers encoder.

    Runs in-process on CPU. Nothing is sent anywhere: uploaded files may hold
    customer data, and an embedding call is a copy of their contents, so the
    encoder staying local is a privacy property rather than a performance
    one.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        dimension: int = DEFAULT_EMBEDDING_DIMENSION,
    ) -> None:
        self.model_name = model_name
        self._dimension = dimension
        self._model = None

    @property
    def dimension(self) -> int:
        return self._dimension

    def _load(self):
        """
        Construct the model on first use.

        The import is inside the function on purpose — see the module
        docstring. Moving it to the top of the file pulls torch into every
        process that imports anything under app.workspace.
        """

        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)

            # Trust the model over the declared constant: a wrong dimension
            # surfaces here rather than as a Qdrant dimension-mismatch error
            # much further downstream.
            #
            # [claude] sentence-transformers 5.x renamed this to
            # get_embedding_dimension() and the old name now emits a
            # FutureWarning. Prefer the new name, fall back to the old, so
            # this works either side of the rename.
            measure = getattr(
                self._model,
                "get_embedding_dimension",
                None,
            ) or self._model.get_sentence_embedding_dimension

            self._dimension = measure()

        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        model = self._load()

        # normalize_embeddings makes cosine similarity a plain dot product,
        # which is what the Qdrant collection is configured for.
        vectors = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        return [list(map(float, vector)) for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


_embedder: SentenceTransformerEmbedder | None = None


def get_embedder() -> SentenceTransformerEmbedder:
    """
    Process-wide embedder.

    Cached because loading the model takes seconds; still lazy, because
    constructing it here would defeat the point of the lazy import above.
    """

    global _embedder

    if _embedder is None:
        _embedder = SentenceTransformerEmbedder()

    return _embedder


__all__ = [
    "DEFAULT_EMBEDDING_DIMENSION",
    "DEFAULT_EMBEDDING_MODEL",
    "Embedder",
    "SentenceTransformerEmbedder",
    "get_embedder",
]
