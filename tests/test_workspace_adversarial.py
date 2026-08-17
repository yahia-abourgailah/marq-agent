"""
[claude] Hostile and malformed input.

The failure mode these guard against is not a crash. A crash is loud and
someone fixes it. The danger is a file that parses into *plausible but wrong*
data — a header taken from the wrong row, a column silently emptied, a
truncated sheet reported as complete — because every downstream number then
carries the error while looking entirely reasonable.

So the standard here is: fail honestly, or parse correctly. Never parse
half-way and stay quiet about it.
"""

from __future__ import annotations

import zipfile

import pytest

from app.workspace.embeddings import Embedder
from app.workspace.ingest import MAX_UPLOAD_BYTES, ingest_file
from app.workspace.query import Filter, aggregate, apply_filters
from app.workspace.readers import read_spreadsheet
from app.workspace.readers.tabular import MAX_COLUMNS
from app.workspace.store import WorkspaceError, WorkspaceStore
from tests.workspace_support import build_csv, build_pdf, build_xlsx


@pytest.fixture
def store(tmp_path):
    return WorkspaceStore(tmp_path / "store")


def ingest(store, name, data, embedder: Embedder | None = None):
    return ingest_file(
        store=store,
        index=None,
        embedder=embedder,
        workspace_id="ws1",
        filename=name,
        data=data,
    )


# ============================================================
# Files that are not what they claim
# ============================================================


def test_empty_upload_is_rejected(store):
    with pytest.raises(WorkspaceError, match="empty"):
        ingest(store, "empty.csv", b"")


def test_a_file_over_the_size_limit_is_rejected_before_parsing(store):
    oversized = b"a,b\n" + b"1,2\n" * (MAX_UPLOAD_BYTES // 4)

    with pytest.raises(WorkspaceError, match="limit"):
        ingest(store, "huge.csv", oversized)


def test_an_unsupported_extension_is_rejected(store):
    with pytest.raises(WorkspaceError, match="Unsupported"):
        ingest(store, "payload.exe", b"MZ\x90\x00")


def test_a_file_with_no_extension_is_rejected(store):
    with pytest.raises(WorkspaceError, match="Unsupported"):
        ingest(store, "README", b"hello")


def test_text_masquerading_as_xlsx_fails_honestly(store):
    """
    An .xlsx that is really a text file must raise, not yield an empty
    workbook the agent would describe as containing nothing.
    """

    with pytest.raises(WorkspaceError, match="Could not read"):
        ingest(store, "fake.xlsx", b"this is definitely not a workbook")


def test_binary_garbage_as_pdf_fails_honestly(store):
    with pytest.raises(WorkspaceError, match="Could not read"):
        ingest(store, "fake.pdf", b"\x00\x01\x02\x03 not a pdf")


def test_a_truncated_pdf_fails_rather_than_returning_partial_pages(
    store, tmp_path
):
    whole = build_pdf(tmp_path / "ok.pdf", ["Page one.", "Page two."])
    data = whole.read_bytes()

    with pytest.raises(WorkspaceError):
        ingest(store, "cut.pdf", data[: len(data) // 3])


def test_a_failed_read_leaves_no_file_behind(store):
    """
    A rejected upload must not litter the workspace — neither a registry
    entry nor the bytes that were written before parsing was attempted.
    """

    with pytest.raises(WorkspaceError):
        ingest(store, "fake.xlsx", b"not a workbook")

    assert store.list_files("ws1") == ()

    raw = store.root / "ws1" / "raw"
    assert list(raw.iterdir()) == [] if raw.exists() else True


def test_an_xlsx_that_is_a_zip_of_nothing_fails(store, tmp_path):
    """A valid zip that is not a workbook must not parse as an empty one."""

    path = tmp_path / "hollow.xlsx"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("nothing.txt", "hello")

    with pytest.raises(WorkspaceError):
        ingest(store, "hollow.xlsx", path.read_bytes())


# ============================================================
# Files that parse, but strangely
# ============================================================


def test_a_header_only_sheet_reports_zero_rows_not_a_phantom_row(tmp_path):
    path = build_csv(tmp_path / "a.csv", header=["id", "amount"], rows=[])

    sheet = read_spreadsheet(path)[0].sheets[0]

    assert sheet.row_count == 0
    assert sheet.column_names == ("id", "amount")


def test_an_all_null_column_is_labelled_empty_not_numeric(tmp_path):
    """
    An empty column must never be summable. Labelling it `number` would let
    the agent report a total of 0 for a column that holds nothing.
    """

    (tmp_path / "a.csv").write_text("id,unused\n1,\n2,\n", encoding="utf-8")

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]
    unused = [c for c in sheet.columns if c.name == "unused"][0]

    assert unused.dtype == "empty"
    assert unused.non_null == 0


def test_an_aggregate_over_an_empty_column_is_none_not_zero(tmp_path):
    (tmp_path / "a.csv").write_text("id,unused\n1,\n2,\n", encoding="utf-8")

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]
    result = aggregate(sheet, "sum", column="unused")

    assert result["value"] is None


def test_absurd_column_counts_are_capped_with_a_warning(tmp_path):
    width = MAX_COLUMNS + 50
    header = ",".join(f"c{i}" for i in range(width))

    (tmp_path / "wide.csv").write_text(
        f"{header}\n{','.join('1' for _ in range(width))}\n", encoding="utf-8"
    )

    parsed, warnings = read_spreadsheet(tmp_path / "wide.csv")

    assert len(parsed.sheets[0].columns) == MAX_COLUMNS
    assert any("columns" in warning for warning in warnings)


def test_ragged_rows_do_not_shift_values_into_the_wrong_columns(tmp_path):
    """
    The dangerous version of a malformed row: values sliding one column left
    would put an amount under a status heading and look completely normal.
    """

    (tmp_path / "a.csv").write_text(
        "id,status,amount\n1,contracted,100\n2,200\n3,eoi,300\n",
        encoding="utf-8",
    )

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    # Row 2 is short; its values fill from the left and the tail is null.
    assert sheet.rows[1]["id"] == "2"
    assert sheet.rows[1]["amount"] is None
    # Row 3 must be unaffected by row 2's shape.
    assert sheet.rows[2] == {"id": "3", "status": "eoi", "amount": "300"}


def test_duplicate_sheet_names_are_both_readable(tmp_path):
    """openpyxl permits it; resolution must not silently pick one."""

    path = build_xlsx(
        tmp_path / "dup.xlsx",
        {"Data": (["id"], [[1]]), "Data ": (["id"], [[2]])},
    )

    parsed, _ = read_spreadsheet(path)

    assert len(parsed.sheets) == 2


def test_unicode_and_rtl_survive_a_round_trip(tmp_path, store):
    """
    The CRM is bilingual, so Arabic column names and values must survive
    parsing, JSON persistence and reload unchanged.
    """

    content = "الاسم,المبلغ\nسارة مصطفى,1000\nأحمد جابر,2500\n"
    (tmp_path / "ar.csv").write_text(content, encoding="utf-8")

    result = ingest(store, "ar.csv", (tmp_path / "ar.csv").read_bytes())
    parsed = store.load_parsed("ws1", result.file.file_id)
    sheet = parsed.sheets[0]

    assert sheet.column_names == ("الاسم", "المبلغ")
    assert sheet.rows[0]["الاسم"] == "سارة مصطفى"
    assert sheet.rows[1]["المبلغ"] == "2500"


def test_a_very_long_cell_is_truncated_rather_than_carried_whole(tmp_path):
    long_value = "x" * 10_000

    (tmp_path / "a.csv").write_text(
        f"id,note\n1,{long_value}\n", encoding="utf-8"
    )

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    assert len(sheet.rows[0]["note"]) < 3_000


def test_a_formula_only_workbook_says_why_it_looks_empty(tmp_path):
    """
    openpyxl reads cached formula results, which a library-written file does
    not have. The agent must not report the file as simply blank.
    """

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["id", "total"])
    sheet.append([1, "=A2*2"])
    workbook.save(tmp_path / "formula.xlsx")

    parsed, warnings = read_spreadsheet(tmp_path / "formula.xlsx")

    # The formula cell has no cached value, so it reads as empty — the point
    # is that this is visible rather than silently zero.
    total = [c for c in parsed.sheets[0].columns if c.name == "total"][0]
    assert total.dtype == "empty"

    # And an aggregate over it refuses rather than returning 0.
    assert aggregate(parsed.sheets[0], "sum", column="total")["value"] is None


# ============================================================
# Hostile filenames and identifiers
# ============================================================


@pytest.mark.parametrize(
    "filename",
    [
        "../../../etc/passwd.csv",
        "..\\..\\windows\\system32\\config.csv",
        "/etc/shadow.csv",
        "....//....//secret.csv",
    ],
)
def test_traversal_filenames_are_stored_safely(store, filename):
    """
    The name is recorded for display and never used as a path, so these
    ingest normally and land under their opaque file id.
    """

    result = ingest(store, filename, b"id,amount\n1,100\n")

    assert result.file.filename == filename

    written = list((store.root / "ws1" / "raw").iterdir())
    assert len(written) == 1
    assert written[0].name.startswith("wf_")
    assert ".." not in written[0].name


def test_a_null_byte_in_a_filename_does_not_reach_the_filesystem(store):
    ingest(store, "evil\x00.csv", b"id\n1\n")

    written = list((store.root / "ws1" / "raw").iterdir())

    assert len(written) == 1
    assert "\x00" not in written[0].name


@pytest.mark.parametrize(
    "workspace",
    ["../escape", "ws/../../root", "..", "", "a" * 100, "ws;rm -rf /"],
)
def test_hostile_workspace_ids_are_rejected(store, workspace):
    with pytest.raises(WorkspaceError):
        ingest_file(
            store=store,
            index=None,
            embedder=None,
            workspace_id=workspace,
            filename="a.csv",
            data=b"id\n1\n",
        )


# ============================================================
# Filters and aggregates on hostile input
# ============================================================


def test_filter_values_are_data_not_expressions(tmp_path):
    """
    Filters are compared, never evaluated. A value that looks like code or
    SQL is just a string that matches nothing.
    """

    (tmp_path / "a.csv").write_text(
        "id,status\n1,contracted\n2,eoi\n", encoding="utf-8"
    )

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    for hostile in ("' OR 1=1 --", "__import__('os').system('ls')", "{{7*7}}"):
        rows = apply_filters(sheet, [Filter("status", "eq", hostile)])
        assert rows == []


def test_a_column_name_that_looks_like_an_attack_is_just_missing(tmp_path):
    (tmp_path / "a.csv").write_text("id\n1\n", encoding="utf-8")

    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    from app.workspace.query import QueryError

    with pytest.raises(QueryError):
        apply_filters(sheet, [Filter("id; DROP TABLE deals", "eq", 1)])


def test_ingestion_survives_a_broken_embedder(store, tmp_path):
    """
    A failing embedder must cost search only. The file still parses and
    stays readable, and the warning says what was lost.
    """

    class BrokenEmbedder:
        dimension = 8

        def embed_documents(self, texts):
            raise RuntimeError("model unavailable")

        def embed_query(self, text):
            raise RuntimeError("model unavailable")

    class DummyIndex:
        def upsert(self, chunks, vectors):
            return len(chunks)

    result = ingest_file(
        store=store,
        index=DummyIndex(),
        embedder=BrokenEmbedder(),
        workspace_id="ws1",
        filename="a.csv",
        data=b"id,amount\n1,100\n",
    )

    assert result.chunks_indexed == 0
    assert any("could not be indexed" in w for w in result.warnings)

    # The file is still fully usable.
    parsed = store.load_parsed("ws1", result.file.file_id)
    assert parsed.sheets[0].row_count == 1


def test_a_pdf_with_no_text_layer_is_not_reported_as_empty(store, tmp_path):
    path = build_pdf(tmp_path / "scan.pdf", ["", "", ""])

    result = ingest(store, "scan.pdf", path.read_bytes())

    assert result.file.page_count == 3
    assert any("scanned images" in w for w in result.warnings)


# ============================================================
# [claude] PDF failure paths — 76% covered, and the uncovered lines were
# every branch that handles a file going wrong.
# ============================================================


def test_an_encrypted_pdf_says_it_is_password_protected(store, tmp_path):
    """
    An encrypted PDF parses fine and yields empty text on every page, which
    is indistinguishable from a scan unless it is named. Telling someone
    their contract "appears to be scanned images" when it is actually
    password-protected sends them to fix the wrong problem.
    """

    from pypdf import PdfReader, PdfWriter

    plain = build_pdf(tmp_path / "plain.pdf", ["Confidential terms."])

    writer = PdfWriter()
    for page in PdfReader(str(plain)).pages:
        writer.add_page(page)
    writer.encrypt("a-password")

    locked = tmp_path / "locked.pdf"
    with open(locked, "wb") as handle:
        writer.write(handle)

    with pytest.raises(WorkspaceError, match="password-protected"):
        ingest(store, "locked.pdf", locked.read_bytes())


def test_an_oversized_page_is_truncated_and_reported(store, tmp_path):
    """
    One pathological page — a generated report with a data dump in it —
    must not consume the whole workspace, and the truncation must be
    visible rather than silently changing what the document says.
    """

    from app.workspace.readers.documents import MAX_PAGE_CHARS

    huge = "word " * (MAX_PAGE_CHARS // 2)
    path = build_pdf(tmp_path / "huge.pdf", [huge])

    result = ingest(store, "huge.pdf", path.read_bytes())

    assert any("truncated" in w for w in result.warnings)

    parsed = store.load_parsed("ws1", result.file.file_id)
    assert len(parsed.pages[0].text) <= MAX_PAGE_CHARS


def test_one_unreadable_page_does_not_lose_the_document(monkeypatch, tmp_path):
    """
    A malformed page should cost that page, not the whole file — and the
    loss must be stated.
    """

    from app.workspace.readers import documents

    path = build_pdf(tmp_path / "mixed.pdf", ["Good page.", "Also good."])

    original = documents.PdfReader

    class OnePageFails:
        def __init__(self, *args, **kwargs):
            self._real = original(*args, **kwargs)
            self.is_encrypted = False

        @property
        def pages(self):
            pages = list(self._real.pages)

            class Broken:
                def extract_text(self_inner):
                    raise RuntimeError("corrupt content stream")

            return [pages[0], Broken()]

    monkeypatch.setattr(documents, "PdfReader", OnePageFails)

    parsed, warnings = documents.read_document(path)

    assert len(parsed.pages) == 2
    assert "Good page" in parsed.pages[0].text
    assert parsed.pages[1].text == ""
    assert any("could not be extracted" in w for w in warnings)


def test_a_pdf_with_no_pages_is_refused(monkeypatch, tmp_path):
    """An empty document must raise, not parse to a file containing nothing."""

    from app.workspace.readers import documents

    path = build_pdf(tmp_path / "ok.pdf", ["Something."])

    original = documents.PdfReader

    class NoPages:
        def __init__(self, *args, **kwargs):
            self._real = original(*args, **kwargs)
            self.is_encrypted = False

        @property
        def pages(self):
            return []

    monkeypatch.setattr(documents, "PdfReader", NoPages)

    with pytest.raises(ValueError, match="no pages"):
        documents.read_document(path)
