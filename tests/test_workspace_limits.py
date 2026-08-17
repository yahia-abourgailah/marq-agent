"""
[claude] Behaviour exactly at the limits, and just past them.

Caps are only useful if crossing one is *visible*. A silently truncated
result is worse than no cap at all: the agent reports a total over the part
it happened to receive, and nothing anywhere says the answer is partial.

So each of these checks two things — the cap holds, and the payload says it
held.
"""

from __future__ import annotations

import pytest

from app.tools.workspace import MAX_COMPARE_ROWS, build_workspace_tools
from app.workspace.compare import MAX_EXAMPLES, compare_rows
from app.workspace.index import build_index
from app.workspace.query import MAX_RETURNED_ROWS, Filter, aggregate
from app.workspace.readers import read_spreadsheet
from app.workspace.service import WorkspaceService
from app.workspace.store import WorkspaceStore
from tests.workspace_support import FakeEmbedder


@pytest.fixture
def service(tmp_path):
    embedder = FakeEmbedder()

    return WorkspaceService(
        store=WorkspaceStore(tmp_path / "store"),
        index=build_index(collection="limits", dimension=embedder.dimension),
        embedder=embedder,
    )


def sheet_with(rows: int, tmp_path):
    lines = ["id,amount"]
    lines += [f"{i},{i}" for i in range(1, rows + 1)]

    path = tmp_path / "big.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return path


# ============================================================
# Row reads
# ============================================================


def test_a_read_at_the_cap_is_flagged_as_truncated(service, tmp_path):
    service.ingest_path("ws", sheet_with(MAX_RETURNED_ROWS + 50, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=MAX_RETURNED_ROWS + 50)

    assert result["matched_rows"] == MAX_RETURNED_ROWS + 50
    assert result["returned_rows"] == MAX_RETURNED_ROWS
    assert result["truncated"] is True


def test_a_read_just_under_the_cap_is_not_flagged(service, tmp_path):
    service.ingest_path("ws", sheet_with(MAX_RETURNED_ROWS - 1, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=MAX_RETURNED_ROWS)

    assert result["truncated"] is False
    assert result["returned_rows"] == MAX_RETURNED_ROWS - 1


def test_exactly_at_the_cap_is_not_falsely_flagged(service, tmp_path):
    """An off-by-one here would tell the user a complete answer is partial."""

    service.ingest_path("ws", sheet_with(MAX_RETURNED_ROWS, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=MAX_RETURNED_ROWS)

    assert result["returned_rows"] == MAX_RETURNED_ROWS
    assert result["truncated"] is False


def test_a_caller_cannot_raise_the_cap(service, tmp_path):
    service.ingest_path("ws", sheet_with(MAX_RETURNED_ROWS + 100, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=10_000)

    assert result["returned_rows"] == MAX_RETURNED_ROWS
    assert result["truncated"] is True


@pytest.mark.parametrize("limit", [0, -1, -999])
def test_a_nonsense_limit_still_returns_something_sane(service, tmp_path, limit):
    service.ingest_path("ws", sheet_with(10, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=limit)

    assert result["returned_rows"] >= 1


# ============================================================
# Aggregates ignore the read cap
# ============================================================


def test_an_aggregate_covers_every_row_not_just_the_readable_ones(
    service, tmp_path
):
    """
    The property the whole design rests on: reads are capped, totals are
    not. If aggregation stopped at the read cap, a big file would silently
    report the sum of its first 200 rows.
    """

    count = MAX_RETURNED_ROWS + 300
    service.ingest_path("ws", sheet_with(count, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.aggregate("ws", file_id, operation="sum", column="amount")

    assert result["matched_rows"] == count
    assert result["value"] == count * (count + 1) / 2


def test_a_grouped_aggregate_also_covers_everything(service, tmp_path):
    count = MAX_RETURNED_ROWS + 300
    service.ingest_path("ws", sheet_with(count, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.aggregate(
        "ws", file_id, operation="count", group_by="amount"
    )

    assert result["matched_rows"] == count


# ============================================================
# Empty and degenerate inputs
# ============================================================


def test_a_filter_matching_nothing_reports_zero_not_an_error(
    service, tmp_path
):
    service.ingest_path("ws", sheet_with(10, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows(
        "ws", file_id, filters=[Filter("amount", "gt", 10**9)]
    )

    assert result["matched_rows"] == 0
    assert result["rows"] == []
    assert result["truncated"] is False


def test_a_csv_sheet_is_named_after_the_upload_not_the_storage_id(
    service, tmp_path
):
    """
    [claude] A delimited file borrows its sheet name from the filename, and
    on disk that is the opaque id — so the agent told users their data lived
    in "sheet wf_21b07b7fe4281016". Found by reading a failing test's dump.
    """

    (tmp_path / "Q3 Tracker.csv").write_text("id\n1\n", encoding="utf-8")
    service.ingest_path("ws", tmp_path / "Q3 Tracker.csv")

    entry = service.list_files("ws")[0]

    assert entry.sheets[0].name == "Q3 Tracker"


def test_passing_raw_dicts_as_filters_says_what_to_do_instead(
    service, tmp_path
):
    """The old failure was an AttributeError several frames deep."""

    from app.workspace.query import QueryError

    service.ingest_path("ws", sheet_with(3, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    with pytest.raises(QueryError, match="Filter objects"):
        service.read_rows(
            "ws", file_id, filters=[{"column": "id", "op": "eq", "value": 1}]
        )


def test_a_single_row_single_column_file_works(service, tmp_path):
    (tmp_path / "tiny.csv").write_text("only\n1\n", encoding="utf-8")
    service.ingest_path("ws", tmp_path / "tiny.csv")

    file_id = service.list_files("ws")[0].file_id
    result = service.aggregate("ws", file_id, operation="sum", column="only")

    assert result["value"] == 1


def test_averaging_nothing_returns_none_rather_than_dividing_by_zero(
    tmp_path,
):
    (tmp_path / "a.csv").write_text("id,amount\n1,10\n", encoding="utf-8")
    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    result = aggregate(
        sheet, "avg", column="amount", filters=[Filter("id", "eq", 999)]
    )

    assert result["value"] is None
    assert result["matched_rows"] == 0


# ============================================================
# Comparison limits
# ============================================================


def test_comparison_counts_stay_exact_far_past_the_example_cap():
    left = [{"id": i, "v": i} for i in range(1, 601)]
    right = [{"id": i, "v": i + 1} for i in range(1, 601)]

    result = compare_rows(left, right, key="id", value_columns=["v"])

    assert result["totals"]["mismatched"] == 600
    assert len(result["mismatched"]) == MAX_EXAMPLES


@pytest.mark.asyncio
async def test_the_compare_payload_ceiling_is_refused_not_silently_trimmed(
    tmp_path,
):
    """
    Trimming would reconcile part of the data and report it as the whole,
    which is the failure this tool exists to prevent.
    """

    service = WorkspaceService(store=WorkspaceStore(tmp_path))
    tools = {t.name: t for t in build_workspace_tools(service)}

    rows = [{"id": i} for i in range(MAX_COMPARE_ROWS + 1)]

    result = await tools["compare_with_crm"].coroutine(
        uploaded_rows=rows, crm_rows=[], key="id"
    )

    assert result["success"] is False
    assert result["retryable"] is True


@pytest.mark.asyncio
async def test_exactly_at_the_compare_ceiling_is_accepted(tmp_path):
    service = WorkspaceService(store=WorkspaceStore(tmp_path))
    tools = {t.name: t for t in build_workspace_tools(service)}

    rows = [{"id": i, "v": 1} for i in range(MAX_COMPARE_ROWS)]

    result = await tools["compare_with_crm"].coroutine(
        uploaded_rows=rows, crm_rows=rows, key="id", value_columns=["v"]
    )

    assert result["success"] is True
    assert result["totals"]["matched"] == MAX_COMPARE_ROWS


# ============================================================
# Search limits
# ============================================================


def test_search_on_an_empty_query_returns_nothing_rather_than_everything(
    service, tmp_path
):
    service.ingest_path("ws", sheet_with(5, tmp_path))

    assert service.search("ws", "") == []
    assert service.search("ws", "   ") == []


def test_search_never_returns_more_than_asked(service, tmp_path):
    service.ingest_path("ws", sheet_with(400, tmp_path))

    assert len(service.search("ws", "amount", limit=3)) <= 3
