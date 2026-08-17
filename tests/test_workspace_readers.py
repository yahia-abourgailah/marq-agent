"""
[claude] Reader behaviour — parsing, type inference and honest warnings.

The type-inference tests carry the weight. A column labelled "number" when it
holds "N/A" among its values is how an average comes out wrong while looking
completely reasonable, which is the file-side version of the denominator bug
family in docs/HANDOFF.md.
"""

from __future__ import annotations

import pytest

from app.workspace.models import FileKind
from app.workspace.readers import read_document, read_spreadsheet
from tests.workspace_support import build_csv, build_pdf, build_xlsx

# ============================================================
# Delimited files
# ============================================================


def test_csv_reads_header_rows_and_types(tmp_path):
    path = build_csv(
        tmp_path / "deals.csv",
        header=["deal_id", "amount", "status"],
        rows=[["D-1", "1000", "contracted"], ["D-2", "2500", "eoi"]],
    )

    parsed, warnings = read_spreadsheet(path)

    assert parsed.kind is FileKind.SPREADSHEET
    assert warnings == ()

    sheet = parsed.sheets[0]

    assert sheet.column_names == ("deal_id", "amount", "status")
    assert sheet.row_count == 2
    assert sheet.rows[0] == {
        "deal_id": "D-1",
        "amount": "1000",
        "status": "contracted",
    }


def test_csv_numbers_are_labelled_numeric_text_not_number(tmp_path):
    """
    Everything in a CSV arrives as text. Labelling it distinctly tells the
    agent a cast is involved, rather than implying the file declared a type
    it never had.
    """

    path = build_csv(
        tmp_path / "a.csv",
        header=["amount"],
        rows=[["1000"], ["2500"]],
    )

    sheet = read_spreadsheet(path)[0].sheets[0]

    assert sheet.columns[0].dtype == "numeric_text"


def test_a_column_with_one_non_numeric_value_is_text(tmp_path):
    """
    [claude] The important negative case. 'N/A' among the numbers must not
    earn the column a numeric label, because the agent would then sum it and
    quietly report a total over fewer rows than it claims.
    """

    path = build_csv(
        tmp_path / "a.csv",
        header=["amount"],
        rows=[["1000"], ["N/A"], ["2500"]],
    )

    sheet = read_spreadsheet(path)[0].sheets[0]

    assert sheet.columns[0].dtype == "text"


def test_leading_blank_rows_are_skipped_before_the_header(tmp_path):
    (tmp_path / "a.csv").write_text(
        "\n\ndeal_id,amount\nD-1,100\n", encoding="utf-8"
    )

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    assert sheet.column_names == ("deal_id", "amount")
    assert sheet.row_count == 1


def test_a_title_row_above_the_header_does_not_become_the_columns(tmp_path):
    """
    [claude] Regression. A real exported tracker opened with a one-cell title,
    a blank line, then the header. The reader took the title row — because it
    was the first row containing anything — and named column 1 after the
    report while columns 2-7 became column_2..column_7. Every column in the
    file was then unaddressable, and an aggregate over "Area (sqm)" failed
    with "no such column".

    The unit tests covered leading blank rows and missed this; the bug came
    back from an actual .xlsx.
    """

    path = build_xlsx(
        tmp_path / "tracker.xlsx",
        {
            "Q3": (
                ["MarQ Sales — Q3 Unit Tracker (internal)"],
                [
                    [],
                    ["Deal ID", "Client", "Area"],
                    [1, "Amal", 195],
                    [2, "Sara", 92],
                ],
            )
        },
    )

    parsed, warnings = read_spreadsheet(path)
    sheet = parsed.sheets[0]

    assert sheet.column_names == ("Deal ID", "Client", "Area")
    assert sheet.row_count == 2
    assert sheet.rows[0] == {"Deal ID": 1, "Client": "Amal", "Area": 195}

    # The skipped title is reported rather than silently dropped.
    assert any("above the header" in warning for warning in warnings)


def test_a_single_column_sheet_still_finds_its_header(tmp_path):
    """
    The header rule picks the widest row, so a genuinely one-column sheet —
    where every row is equally narrow — must still resolve to its first row.
    """

    (tmp_path / "notes.csv").write_text(
        "Note\nAreas confirmed.\nTwo units pending.\n", encoding="utf-8"
    )

    sheet = read_spreadsheet(tmp_path / "notes.csv")[0].sheets[0]

    assert sheet.column_names == ("Note",)
    assert sheet.row_count == 2


def test_the_header_wins_over_an_equally_wide_data_row(tmp_path):
    """Earliest-wins on ties keeps the header from sliding onto row 1 of data."""

    (tmp_path / "a.csv").write_text(
        "id,name,area\n1,Amal,195\n2,Sara,92\n", encoding="utf-8"
    )

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    assert sheet.column_names == ("id", "name", "area")
    assert sheet.row_count == 2


def test_blank_and_duplicate_headers_become_addressable_names(tmp_path):
    (tmp_path / "a.csv").write_text(
        "id,,id\n1,2,3\n", encoding="utf-8"
    )

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    assert sheet.column_names == ("id", "column_2", "id_2")

    # Values stay as text: a CSV declares no types, and parsing "007" into 7
    # would destroy reference codes.
    assert sheet.rows[0] == {"id": "1", "column_2": "2", "id_2": "3"}


def test_fully_blank_rows_are_dropped(tmp_path):
    (tmp_path / "a.csv").write_text(
        "id\n1\n\n\n2\n", encoding="utf-8"
    )

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    assert sheet.row_count == 2


def test_short_rows_are_padded_rather_than_misaligned(tmp_path):
    (tmp_path / "a.csv").write_text(
        "a,b,c\n1,2\n", encoding="utf-8"
    )

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    assert sheet.rows[0] == {"a": "1", "b": "2", "c": None}


def test_tsv_uses_tabs(tmp_path):
    (tmp_path / "a.tsv").write_text("a\tb\n1\t2\n", encoding="utf-8")

    sheet = read_spreadsheet(tmp_path / "a.tsv")[0].sheets[0]

    assert sheet.column_names == ("a", "b")


def test_a_utf8_bom_does_not_become_part_of_the_first_column_name(tmp_path):
    """Excel writes a BOM on CSV export; it must not make column 1 unnameable."""

    (tmp_path / "a.csv").write_bytes("deal_id,amount\nD-1,5\n".encode("utf-8-sig"))

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    assert sheet.column_names == ("deal_id", "amount")


def test_non_utf8_decodes_with_a_warning(tmp_path):
    (tmp_path / "a.csv").write_bytes("name\nCaf\xe9\n".encode("cp1252"))

    parsed, warnings = read_spreadsheet(tmp_path / "a.csv")

    assert parsed.sheets[0].row_count == 1
    assert any("not valid UTF-8" in warning for warning in warnings)


# ============================================================
# Workbooks
# ============================================================


def test_xlsx_reads_every_worksheet(tmp_path):
    path = build_xlsx(
        tmp_path / "book.xlsx",
        {
            "Deals": (["id", "amount"], [[1, 1000], [2, 2500]]),
            "Leads": (["id", "source"], [[9, "meta"]]),
        },
    )

    parsed, warnings = read_spreadsheet(path)

    assert [sheet.name for sheet in parsed.sheets] == ["Deals", "Leads"]
    assert parsed.sheets[0].row_count == 2
    assert parsed.sheets[1].rows[0] == {"id": 9, "source": "meta"}


def test_xlsx_numeric_columns_are_labelled_number(tmp_path):
    """Excel carries real types, unlike CSV, so the label is stronger."""

    path = build_xlsx(
        tmp_path / "book.xlsx",
        {"S": (["amount"], [[1000], [2500]])},
    )

    sheet = read_spreadsheet(path)[0].sheets[0]

    assert sheet.columns[0].dtype == "number"
    assert sheet.rows[0]["amount"] == 1000


def test_xlsx_dates_become_iso_strings(tmp_path):
    from datetime import datetime

    path = build_xlsx(
        tmp_path / "book.xlsx",
        {"S": (["closed_at"], [[datetime(2026, 3, 4, 9, 30)]])},
    )

    sheet = read_spreadsheet(path)[0].sheets[0]

    assert sheet.rows[0]["closed_at"].startswith("2026-03-04T09:30")
    assert sheet.columns[0].dtype == "date"


def test_an_empty_worksheet_is_skipped_with_a_warning(tmp_path):
    path = build_xlsx(
        tmp_path / "book.xlsx",
        {"Real": (["id"], [[1]]), "Empty": ([], [])},
    )

    parsed, warnings = read_spreadsheet(path)

    assert [sheet.name for sheet in parsed.sheets] == ["Real"]
    assert any("Empty" in warning for warning in warnings)


# ============================================================
# Documents
# ============================================================


def test_pdf_reads_one_entry_per_page_with_numbers(tmp_path):
    path = build_pdf(
        tmp_path / "contract.pdf",
        ["Payment terms are net 30 days.", "Penalties apply after 60 days."],
    )

    parsed, warnings = read_document(path)

    assert parsed.kind is FileKind.DOCUMENT
    assert [page.number for page in parsed.pages] == [1, 2]
    assert "net 30" in parsed.pages[0].text
    assert "Penalties" in parsed.pages[1].text
    assert warnings == ()


def test_a_page_with_no_text_is_reported_not_hidden(tmp_path):
    path = build_pdf(tmp_path / "a.pdf", ["Real text here.", ""])

    parsed, warnings = read_document(path)

    assert len(parsed.pages) == 2
    assert any("page(s): 2" in warning for warning in warnings)


def test_a_document_with_no_text_at_all_says_it_is_probably_scanned(tmp_path):
    """
    [claude] "Scanned images" and "empty document" are different things to
    tell a user, and only one of them is true. Getting this wrong makes the
    agent report that a contract contains nothing.
    """

    path = build_pdf(tmp_path / "a.pdf", ["", ""])

    _, warnings = read_document(path)

    assert any("scanned images" in warning for warning in warnings)


def test_an_unreadable_file_raises_rather_than_returning_nothing(tmp_path):
    """
    A file that cannot be parsed must fail loudly. Returning an empty
    document would have the agent report that the file contains nothing.
    """

    (tmp_path / "broken.pdf").write_bytes(b"not a pdf at all")

    with pytest.raises(ValueError, match="Could not read PDF"):
        read_document(tmp_path / "broken.pdf")


# ============================================================
# [claude] Ruled tables inside PDFs — the exact lane for documents.
#
# A figure in a PDF used to be quotable but not computable: retrieval could
# find it and nothing could total it. That is fine on a five-line schedule
# and silently samples a five-hundred-line one, because search returns the
# passages most like the question rather than every row meeting a condition.
# ============================================================


def test_a_ruled_table_in_a_pdf_becomes_a_queryable_sheet(tmp_path):
    from tests.workspace_support import build_ruled_table_pdf

    path = build_ruled_table_pdf(
        tmp_path / "schedule.pdf",
        header=["Deal ID", "Unit", "Area"],
        rows=[[1, "U-207", 210], [2, "U-238", 88], [4, "U-129", 195]],
    )

    parsed, _ = read_document(path)

    assert parsed.kind is FileKind.DOCUMENT
    assert parsed.pages, "the prose must still be readable"
    assert len(parsed.sheets) == 1

    sheet = parsed.sheets[0]

    assert sheet.name == "page 1 table 1"
    assert sheet.column_names == ("Deal ID", "Unit", "Area")
    assert sheet.row_count == 3
    assert sheet.rows[0]["Unit"] == "U-207"


def test_a_pdf_table_can_be_totalled_exactly(tmp_path):
    """The whole point: computed over every row, not sampled."""

    from app.workspace.query import aggregate
    from tests.workspace_support import build_ruled_table_pdf

    path = build_ruled_table_pdf(
        tmp_path / "schedule.pdf",
        header=["Unit", "Area"],
        rows=[["U-1", 210], ["U-2", 88], ["U-3", 195], ["U-4", 468]],
    )

    sheet = read_document(path)[0].sheets[0]
    result = aggregate(sheet, "sum", column="Area")

    assert result["value"] == 961  # 210 + 88 + 195 + 468
    assert result["matched_rows"] == 4


def test_prose_is_never_mistaken_for_a_table(tmp_path):
    """
    [claude] The reason only ruled tables are accepted. pdfplumber will also
    infer tables from text alignment, and on a plain contract it "found" one
    on every page and split words mid-token — 'SCHEDULE A - UN', 'ITS RE'.
    Half a table is worse than none, because it looks like data.
    """

    path = build_pdf(
        tmp_path / "contract.pdf",
        [
            "SECTION 1 - PAYMENT TERMS\n"
            "A reservation deposit of ten percent is payable on signature.\n"
            "The balance is due within thirty days of the invoice date.",
            "SCHEDULE A - UNITS RESERVED\n"
            "Deal 1, unit U-207, gross area 210 square metres.\n"
            "Deal 2, unit U-238, gross area 88 square metres.",
        ],
    )

    parsed, _ = read_document(path)

    assert parsed.sheets == ()
    assert len(parsed.pages) == 2


def test_a_document_with_no_tables_refuses_row_reads_and_says_why(tmp_path):
    from app.workspace.index import build_index
    from app.workspace.query import QueryError
    from app.workspace.service import WorkspaceService
    from app.workspace.store import WorkspaceStore
    from tests.workspace_support import FakeEmbedder

    embedder = FakeEmbedder()
    service = WorkspaceService(
        store=WorkspaceStore(tmp_path / "store"),
        index=build_index(collection="notables", dimension=embedder.dimension),
        embedder=embedder,
    )

    path = build_pdf(tmp_path / "prose.pdf", ["Just some prose here."])
    file_id = service.ingest_path("ws", path).file.file_id

    with pytest.raises(QueryError, match="no tables"):
        service.read_rows("ws", file_id)
