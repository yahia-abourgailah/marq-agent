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

# [claude] The relevance floor for this encoder, calibrated rather than
# adopted. Measured against a real seven-column deals export:
#
#     topical queries     top hit 0.304 - 0.448
#     unrelated queries   top hit 0.024 - 0.172
#
# The 24 August review suggested "somewhere around 0.35-0.45" and said to
# measure rather than take the number, which mattered: 0.35 would have
# discarded "unit area in square metres", a question the sheet answers,
# whose best hit is 0.304.
#
# 0.25 clears every noise hit measured and sits under every topical one.
# Re-measure if the model changes — this describes a score distribution,
# not a fact about cosine similarity.
MIN_RELEVANCE_SCORE = 0.25


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

    @property
    def min_relevance_score(self) -> float:
        """
        [claude] The cosine score below which this encoder's hits are noise.

        On the encoder rather than in the search code, because it describes
        *this model's* score distribution and nothing more general. A
        different encoder has a different one, and the fake used in the
        hermetic tests has none at all — it is a token hash whose scores
        are arbitrary, and measured against it an unrelated query outscores
        a topical one. Reading the floor from the model is what keeps a
        number calibrated for one encoder from being applied to another.
        """

    def count_tokens(self, text: str) -> int:
        """
        [claude] Word-pieces this encoder would read `text` as.

        Chunking needs this and cannot get it from character counts. The
        input window is measured in word-pieces, and the ratio to
        characters is not a constant: measured against this model, English
        prose runs about 4.9 characters per piece, Arabic about 4.1, and a
        rendered spreadsheet row about 2.6 — so one character limit is
        either wrong for Arabic or wasteful for English, and wrong for
        spreadsheets in every case.
        """


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

    @property
    def min_relevance_score(self) -> float:
        """
        Calibrated against a real uploaded export — see
        MIN_RELEVANCE_SCORE in index.py for the measurements.
        """

        return MIN_RELEVANCE_SCORE

    def count_tokens(self, text: str) -> int:
        """
        [claude] The tokenizer's own count, not an estimate.

        `max_seq_length` is enforced by truncation rather than by an error:
        text past the window is silently dropped before it is embedded, so
        an oversized chunk is not a failure anybody sees — it is a chunk
        whose tail was never searchable. Measuring here is what lets
        chunking guarantee that cannot happen.
        """

        model = self._load()

        return len(model.tokenizer(text)["input_ids"])

    @property
    def max_input_tokens(self) -> int:
        """The encoder's input window, in word-pieces."""

        return int(getattr(self._load(), "max_seq_length", 128) or 128)

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
