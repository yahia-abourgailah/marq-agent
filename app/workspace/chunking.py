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

# ============================================================
# Chunk size
# ============================================================
#
# [claude] Rewritten 24 August 2026, from measurement rather than
# arithmetic.
#
# The old constants were 700 characters and 20 rows, justified in a comment
# as "roughly 200 tokens, which sits well inside the 128-token window". The
# comment named the right constraint and got the conversion wrong, and the
# encoder truncates rather than raising — so nothing failed. The tails just
# were never embedded, and the chunks retrieved on their openings.
#
# Measured against this encoder's own tokenizer:
#
#     english prose            621 chars  = 128 word-pieces
#     arabic prose             522 chars  = 128 word-pieces
#     rendered sheet rows      152 chars  =  58 word-pieces   (one row)
#
# So 700 characters of English is ~143 pieces and loses its last sixth;
# Arabic loses more, and the model was chosen *because* the CRM carries
# Arabic. The spreadsheet lane was furthest out by a wide margin: twenty
# real rows from an uploaded export measured 1,007 word-pieces against a
# 128 window, so roughly the first two rows of every twenty were embedded
# and the other eighteen were not searchable at all.
#
# The ratio is not a constant — about 4.9 characters per piece for English,
# 4.1 for Arabic, 2.6 for a rendered row — which is why a single character
# limit cannot be right for all three lanes. Chunking is therefore packed
# against a real token count when one is available, and falls back to these
# characters when it is not.

# The encoder's input window, less room for the special tokens it adds.
MAX_CHUNK_TOKENS = 128
TOKEN_SAFETY_MARGIN = 8
CHUNK_TOKEN_BUDGET = MAX_CHUNK_TOKENS - TOKEN_SAFETY_MARGIN

# Character fallbacks, used when no tokenizer is supplied — the hermetic
# tests chunk without loading a 400 MB model. Set from the Arabic figure
# above, because the safe limit is the smallest one, with margin.
DOCUMENT_CHUNK_CHARS = 450
DOCUMENT_OVERLAP_CHARS = 80

# Rows per spreadsheet chunk, when packing by characters is all that is
# available. Two, because that is what measurement showed fits.
ROWS_PER_CHUNK = 2


class ChunkBudget:
    """
    [claude] Decides whether a piece of text fits one embedding chunk.

    One object so that both lanes ask the same question, and so the
    token-aware and character-only paths cannot drift apart.

    `count_tokens` is optional on purpose. Ingestion has the encoder and
    passes it; the hermetic tests do not, and requiring one would mean
    every chunking test loads a real transformer to assert something about
    paragraph boundaries.
    """

    def __init__(self, count_tokens=None, char_limit: int = DOCUMENT_CHUNK_CHARS):
        self._char_limit = char_limit
        self._count = None

        # [claude] Proved once, here, rather than trusted per call.
        #
        # A counter that raises or returns something uncomparable would
        # otherwise fail deep inside the packing loop, and the failure
        # surfaces as a rejected upload rather than as anything a reader
        # would connect to tokenisation. Falling back to characters loses
        # precision and keeps ingestion working, which is the right way
        # round: the file is already persisted by this point.
        if count_tokens is not None:
            try:
                if isinstance(count_tokens("probe"), int):
                    self._count = count_tokens
            except Exception:
                self._count = None

    @property
    def measured(self) -> bool:
        return self._count is not None

    def fits(self, text: str) -> bool:
        if self._count is None:
            return len(text) <= self._char_limit

        return self._count(text) <= CHUNK_TOKEN_BUDGET

    def longest_prefix(self, text: str) -> int:
        """
        How much of `text` fits, in characters.

        Binary search rather than a ratio, because the ratio is the thing
        that was wrong before. Costs about eight tokenizer calls on a
        paragraph that needs hard-splitting, which happens rarely.
        """

        if self.fits(text):
            return len(text)

        if self._count is None:
            return self._char_limit

        low, high = 1, len(text)

        while low < high:
            mid = (low + high + 1) // 2
            if self.fits(text[:mid]):
                low = mid
            else:
                high = mid - 1

        return low


def build_chunks(
    entry: WorkspaceFile,
    parsed: ParsedFile,
    count_tokens=None,
) -> list[Chunk]:
    """
    Produce every chunk for one parsed file.

    [claude] `count_tokens` is the encoder's own tokenizer when the caller
    has one. Given it, chunks are packed against the real input window;
    without it, against the character fallbacks above. See ChunkBudget.
    """

    budget = ChunkBudget(count_tokens)

    if parsed.kind is FileKind.DOCUMENT:
        return _document_chunks(entry, parsed, budget)

    return _sheet_chunks(entry, parsed, budget)


# ============================================================
# Documents
# ============================================================


def _document_chunks(
    entry: WorkspaceFile, parsed: ParsedFile, budget: ChunkBudget
) -> list[Chunk]:
    chunks: list[Chunk] = []

    for page in parsed.pages:
        if not page.text.strip():
            continue

        for position, segment in enumerate(_split_text(page.text, budget)):
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


def _split_text(text: str, budget: ChunkBudget | None = None) -> list[str]:
    """
    Split a page into overlapping segments on paragraph boundaries.

    [claude] Overlap exists so a sentence straddling a boundary is fully
    present in one of the two segments. Without it, the single most
    quotable line on a page can end up half in each and retrieve as neither.

    Segments are sized by `budget` — the encoder's real token window where
    one is available. Every boundary decision below asks `budget.fits`
    rather than comparing character counts, so the Arabic and English paths
    need no separate constants.
    """

    budget = budget or ChunkBudget()
    text = text.strip()

    if budget.fits(text):
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
        # A single paragraph larger than the window is hard-split; there is
        # no smaller natural boundary to use.
        while not budget.fits(paragraph):
            if current:
                segments.append(current)
                current = ""

            cut = budget.longest_prefix(paragraph)
            segments.append(paragraph[:cut])

            # Overlap proportional to the cut, so a sentence on the seam
            # survives whole in one of the two segments.
            step = max(1, cut - min(DOCUMENT_OVERLAP_CHARS, cut // 4))
            paragraph = paragraph[step:]

            if not paragraph.strip():
                paragraph = ""
                break

        if not paragraph:
            continue

        if not current:
            current = paragraph
        elif budget.fits(f"{current}\n\n{paragraph}"):
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


def _sheet_chunks(
    entry: WorkspaceFile, parsed: ParsedFile, budget: ChunkBudget
) -> list[Chunk]:
    """
    [claude] Rows are packed to the window rather than counted to a
    constant.

    A fixed row count cannot be right, because the cost of a row is the
    number of columns times their content — the same twenty rows are a
    comfortable chunk in a three-column sheet and eight times the window in
    a twelve-column one. Packing adapts to the sheet instead of assuming a
    shape, which is also why the old `ROWS_PER_CHUNK = 20` was so far out
    on the real export that prompted this: 1,007 word-pieces against 128.
    """

    chunks: list[Chunk] = []

    for sheet in parsed.sheets:
        for block, start in _row_blocks(sheet, budget):
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

            # [claude] One row can be wider than the window on its own —
            # nine columns of identifiers measured 189 word-pieces against
            # 128 — and there is no smaller row to fall back to. Such a row
            # is split across chunks that share its locator, so every
            # column is searchable and the citation still points at the row
            # the value is actually in. Emitting it whole would embed its
            # leading columns and silently drop the rest.
            for position, piece in enumerate(
                _fit_pieces(_render_rows(sheet, block), sheet.name, budget)
            ):
                suffix = "" if position == 0 else f":{position}"

                chunks.append(
                    Chunk(
                        chunk_id=(
                            f"{entry.file_id}:{sheet.name}:{row_start}{suffix}"
                        ),
                        workspace_id=entry.workspace_id,
                        text=piece,
                        locator=locator,
                    )
                )

    return chunks


def _fit_pieces(text: str, sheet_name: str, budget: ChunkBudget) -> list[str]:
    """
    Cut `text` into pieces that each fit the window.

    Normally returns one piece — packing has already sized the block. The
    loop exists for the single row too wide to pack. Continuation pieces
    repeat the sheet name, because a fragment that has lost it reads as
    values from nowhere and the sheet name is often the most searchable
    thing on the row.
    """

    if budget.fits(text):
        return [text]

    pieces: list[str] = []
    header = f"Sheet: {sheet_name}\n"
    remaining = text

    while remaining and not budget.fits(remaining):
        cut = budget.longest_prefix(remaining)

        if cut <= 0:
            break

        pieces.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()

        if remaining:
            remaining = header + remaining

    if remaining:
        pieces.append(remaining)

    return pieces or [text]


def _row_blocks(sheet: SheetContent, budget: ChunkBudget):
    """
    Greedily group rows into blocks that each fit one chunk.

    A block always holds at least one row. A single row wider than the
    window is emitted anyway and will be truncated by the encoder — a
    truncated row still carries its sheet name and leading columns, which
    retrieves approximately, where emitting nothing retrieves not at all.
    """

    start = 0
    total = len(sheet.rows)

    while start < total:
        taken = 1

        while start + taken < total:
            candidate = sheet.rows[start : start + taken + 1]

            if not budget.fits(_render_rows(sheet, candidate)):
                break

            taken += 1

        yield sheet.rows[start : start + taken], start
        start += taken


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
