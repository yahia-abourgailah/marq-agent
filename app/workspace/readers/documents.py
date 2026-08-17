"""
[claude] PDF reader — text, one page at a time.

Page-at-a-time rather than whole-document
-----------------------------------------
The page number is the citation. A user asking "where does it say that?"
needs somewhere to look, and a document flattened into one string cannot
answer it. Keeping pages separate also bounds the damage from one unreadable
page: the rest of the document still reads.

Scanned PDFs
------------
A page with no text layer yields an empty string. That is reported as a
warning rather than silently dropped, because "this document appears to be
scanned images" and "this document is empty" are very different things to
tell a user, and only one of them is true.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.workspace.models import FileKind, PageContent, ParsedFile, SheetContent
from app.workspace.readers.tabular import _infer_column, _normalise_header

# A page of dense prose is roughly 3,000 characters. The ceiling exists to
# stop one pathological page — a generated report with an embedded data
# dump — from dominating a workspace.
MAX_PAGE_CHARS = 20_000


def read_document(path: Path) -> tuple[ParsedFile, tuple[str, ...]]:
    """Parse a PDF into pages of text."""

    warnings: list[str] = []

    try:
        reader = PdfReader(str(path))
    except PdfReadError as exc:
        raise ValueError(f"Could not read PDF: {exc}") from exc

    # [claude] Encrypted PDFs parse but fail on every page, which surfaces as
    # pypdf's "File has not been decrypted" — accurate and unhelpful. Say
    # which problem it is, because "password-protected" and "scanned images"
    # send someone to fix completely different things.
    #
    # Checked by return value, not by exception: `decrypt` reports a wrong
    # password with a falsy PasswordType rather than raising, so the original
    # try/except here never fired and this message was unreachable.
    if getattr(reader, "is_encrypted", False):
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = False

        if not unlocked:
            raise ValueError(
                "This PDF is password-protected and cannot be read."
            )

    pages: list[PageContent] = []
    empty_pages: list[int] = []

    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            # One malformed page should not cost the whole document.
            text = ""
            warnings.append(f"Page {index} could not be extracted.")

        text = text.strip()

        if len(text) > MAX_PAGE_CHARS:
            text = text[:MAX_PAGE_CHARS]
            warnings.append(
                f"Page {index} was truncated at {MAX_PAGE_CHARS} characters."
            )

        if not text:
            empty_pages.append(index)

        pages.append(PageContent(number=index, text=text))

    if not pages:
        raise ValueError("This PDF has no pages.")

    sheets, table_warnings = _extract_ruled_tables(path)
    warnings.extend(table_warnings)

    if len(empty_pages) == len(pages):
        warnings.append(
            "No text layer was found on any page — this document is "
            "probably scanned images, and its contents cannot be read."
        )
    elif empty_pages:
        listed = ", ".join(str(number) for number in empty_pages[:10])
        warnings.append(f"No text found on page(s): {listed}.")

    parsed = ParsedFile(
        kind=FileKind.DOCUMENT,
        pages=tuple(pages),
        sheets=tuple(sheets),
    )

    return parsed, tuple(warnings)


# ============================================================
# Ruled tables
# ============================================================
#
# [claude] A figure inside a PDF used to be quotable but not computable:
# retrieval could find it, and nothing could total it. That worked on a
# five-line schedule and would silently sample a five-hundred-line one,
# because search returns the passages most like the question rather than
# every row meeting a condition.
#
# So a detected table becomes a real sheet and joins the exact lane —
# workspace_read_rows and workspace_aggregate then work on it, with the same
# completeness guarantees a spreadsheet gets.
#
# ONLY tables drawn with ruling lines are accepted. pdfplumber will also
# infer tables from text alignment, and on plain prose it "found" a table on
# every page of a contract and split words mid-token ('SCHEDULE A - UN',
# 'ITS RE'). Half a table is worse than none: it looks like data. A ruled
# grid is unambiguous, and it is what exported reports actually contain.


def _extract_ruled_tables(path: Path) -> tuple[list[SheetContent], list[str]]:
    """Every ruled table in the document, as queryable sheets."""

    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - optional dependency
        return [], []

    sheets: list[SheetContent] = []
    warnings: list[str] = []

    try:
        with pdfplumber.open(str(path)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                try:
                    tables = page.extract_tables()
                except Exception:
                    warnings.append(
                        f"Tables on page {number} could not be read."
                    )
                    continue

                for position, table in enumerate(tables, start=1):
                    sheet, reason = _table_to_sheet(table, number, position)

                    if sheet is not None:
                        sheets.append(sheet)
                    elif reason:
                        warnings.append(reason)
    except Exception:
        # Table extraction is a bonus; never let it cost the document.
        return sheets, warnings

    return sheets, warnings


def _table_to_sheet(
    table: list[list],
    page_number: int,
    position: int,
) -> tuple[SheetContent | None, str | None]:
    """
    Convert one detected table, or explain why it was rejected.

    Rejection is deliberate and loud. A table the reader is not confident
    about must not become half a sheet the agent would aggregate.
    """

    rows = [row for row in table if row and any(_filled(c) for c in row)]

    if len(rows) < 2:
        return None, None

    header = rows[0]
    width = len(header)

    if width < 2:
        return None, None

    name = f"page {page_number} table {position}"

    if any(len(row) != width for row in rows[1:]):
        return None, (
            f"A table on page {page_number} has rows of differing widths and "
            "was not read as data; its numbers cannot be totalled."
        )

    column_names = _normalise_header(
        tuple(str(cell).replace("\n", " ").strip() if cell else "" for cell in header)
    )

    records = []
    for row in rows[1:]:
        values = [
            str(cell).replace("\n", " ").strip() if cell is not None else None
            for cell in row
        ]
        records.append(dict(zip(column_names, values, strict=True)))

    columns = tuple(_infer_column(name_, records) for name_ in column_names)

    return SheetContent(name=name, columns=columns, rows=tuple(records)), None


def _filled(cell) -> bool:
    return cell is not None and str(cell).strip() != ""


__all__ = ["MAX_PAGE_CHARS", "read_document"]
