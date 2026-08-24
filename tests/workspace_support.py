"""
[claude] Builders and fakes for the workspace tests.

Kept out of conftest.py deliberately: these are imported by name from the
workspace test modules, so a reader of one of those files can see where its
fixtures come from. Nothing here changes behaviour for the existing suite.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from openpyxl import Workbook

from app.workspace.embeddings import Embedder


class FakeEmbedder(Embedder):
    """
    A deterministic stand-in for the sentence-transformer.

    [claude] Hashes tokens into a small vector. It is not semantic, and it is
    not trying to be — the tests it serves are about isolation, citation and
    plumbing, none of which should depend on a 470MB model download or on
    what a real encoder considers similar. Retrieval *quality* is not
    unit-testable and belongs in the eval suite.

    Identical text always embeds identically, and text sharing tokens lands
    nearer, which is enough for "the right chunk comes back first".
    """

    def __init__(self, dimension: int = 32) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def count_tokens(self, text: str) -> int:
        """
        [claude] A real implementation, not the Protocol's stub.

        This class subclasses `Embedder`, so when `count_tokens` was added
        to the protocol it inherited a body that returns `None` — and
        chunking, which asks the embedder for a token count, started
        comparing `None` to an integer. Thirty-nine tests failed at once,
        none of them about embedding.

        Worth the note because the trap is general: adding a method to a
        Protocol that concrete classes *subclass* silently gives every one
        of them a None-returning implementation. The count here is a rough
        word-piece stand-in — not accurate, but the right shape, which is
        all the plumbing tests need.
        """

        return max(1, len(text) // 4)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimension

        for token in text.lower().split():
            vector[hash(token) % self._dimension] += 1.0

        magnitude = sum(value * value for value in vector) ** 0.5

        if magnitude == 0:
            # An all-zero vector has no direction, and Qdrant rejects it for
            # cosine distance. Give empty text a fixed arbitrary direction.
            vector[0] = 1.0
            return vector

        return [value / magnitude for value in vector]


# ============================================================
# File builders
# ============================================================


def build_csv(path: Path, rows: list[list], header: list[str]) -> Path:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)

    path.write_text(buffer.getvalue(), encoding="utf-8")

    return path


def build_xlsx(
    path: Path,
    sheets: dict[str, tuple[list[str], list[list]]],
) -> Path:
    """Build a workbook from {sheet_name: (header, rows)}."""

    workbook = Workbook()
    workbook.remove(workbook.active)

    for name, (header, rows) in sheets.items():
        worksheet = workbook.create_sheet(title=name)
        worksheet.append(header)

        for row in rows:
            worksheet.append(row)

    workbook.save(path)

    return path


def build_ruled_table_pdf(
    path: Path,
    header: list[str],
    rows: list[list],
    title: str = "UNIT SCHEDULE",
) -> Path:
    """
    A PDF containing a table drawn with actual ruling lines.

    [claude] The distinction matters. Text-aligned columns cannot be told
    apart from prose — pdfplumber's text strategy "found" a table on every
    page of a plain contract and split words mid-token. A ruled table is
    unambiguous, which is why the reader accepts only those.

    Emits a stroked grid plus the cell text positioned inside it, which is
    what an exported report from Excel or a reporting tool looks like.
    """

    left, top = 60, 740
    col_width, row_height = 110, 22
    columns = len(header)
    table_rows = [header, *[[str(cell) for cell in row] for row in rows]]

    parts = [f"BT /F1 13 Tf {left} {top + 30} Td ({_escape(title)}) Tj ET"]

    # The grid: one stroked rectangle per cell.
    for r in range(len(table_rows) + 1):
        y = top - r * row_height
        parts.append(
            f"{left} {y} m {left + columns * col_width} {y} l S"
        )
    for c in range(columns + 1):
        x = left + c * col_width
        parts.append(
            f"{x} {top} m {x} {top - len(table_rows) * row_height} l S"
        )

    # The text, placed inside each cell.
    for r, row in enumerate(table_rows):
        for c, cell in enumerate(row):
            x = left + c * col_width + 4
            y = top - (r + 1) * row_height + 7
            parts.append(
                f"BT /F1 9 Tf {x} {y} Td ({_escape(str(cell))}) Tj ET"
            )

    return _write_pdf(path, ["\n".join(parts)])


def build_pdf(path: Path, pages: list[str]) -> Path:
    """
    Write a minimal but valid PDF whose pages contain extractable text.

    [claude] Written by hand rather than with a PDF library, because adding
    reportlab as a test-only dependency to produce three lines of text is a
    poor trade. The structure is the smallest one pypdf will parse: a
    catalogue, a page tree, one page and one content stream each, and a
    correct xref table with byte offsets.
    """

    streams = []

    for text in pages:
        lines = text.split("\n")
        drawn = "\n".join(f"({_escape(line)}) Tj 0 -16 Td" for line in lines)
        streams.append(f"BT /F1 12 Tf 72 720 Td\n{drawn}\nET")

    return _write_pdf(path, streams)


def _write_pdf(path: Path, streams: list[str]) -> Path:
    """
    Assemble a minimal valid PDF from ready-made content streams.

    Shared by the prose and ruled-table builders — the object graph and xref
    arithmetic are the fiddly part and are worth writing once.
    """

    objects: list[bytes] = []

    page_count = len(streams)

    # 1: catalogue, 2: page tree, 3: font.
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")

    kids = " ".join(f"{4 + index * 2} 0 R" for index in range(page_count))
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode()
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )

    for index, content in enumerate(streams):
        page_number = 4 + index * 2
        content_number = page_number + 1

        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_number} 0 R >>"
            ).encode()
        )

        stream = content.encode()

        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []

    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)

    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"

    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()

    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()

    path.write_bytes(bytes(out))

    return path


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


__all__ = [
    "FakeEmbedder",
    "build_csv",
    "build_pdf",
    "build_ruled_table_pdf",
    "build_xlsx",
]
