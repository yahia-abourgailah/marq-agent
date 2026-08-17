"""
[claude] Exact reads and aggregates over parsed spreadsheets.

Every expected value here is computed by hand in the test. That is the point:
these functions are what the agent's numbers come from, so they are checked
against arithmetic rather than against themselves.
"""

from __future__ import annotations

import pytest

from app.workspace.models import ColumnSpec, FileKind, ParsedFile, SheetContent
from app.workspace.query import (
    Filter,
    QueryError,
    aggregate,
    apply_filters,
    get_sheet,
    require_column,
)

ROWS = (
    {"deal_id": "D-1", "amount": 1000, "status": "contracted", "owner": "amal"},
    {"deal_id": "D-2", "amount": 2500, "status": "contracted", "owner": "sara"},
    {"deal_id": "D-3", "amount": 400, "status": "eoi", "owner": "amal"},
    {"deal_id": "D-4", "amount": None, "status": "eoi", "owner": None},
)

SHEET = SheetContent(
    name="Deals",
    columns=(
        ColumnSpec("deal_id", "text", 4),
        ColumnSpec("amount", "number", 3),
        ColumnSpec("status", "text", 4),
        ColumnSpec("owner", "text", 3),
    ),
    rows=ROWS,
)

PARSED = ParsedFile(kind=FileKind.SPREADSHEET, sheets=(SHEET,))


# ============================================================
# Resolution
# ============================================================


def test_a_single_sheet_resolves_without_being_named():
    assert get_sheet(PARSED, None).name == "Deals"


def test_several_sheets_require_a_name_rather_than_guessing():
    """
    [claude] Falling back to the first sheet answers a question about the
    wrong data and looks exactly like a correct answer.
    """

    parsed = ParsedFile(
        kind=FileKind.SPREADSHEET,
        sheets=(SHEET, SheetContent(name="Leads", columns=(), rows=())),
    )

    with pytest.raises(QueryError, match="several worksheets"):
        get_sheet(parsed, None)


def test_an_unknown_sheet_name_lists_the_real_ones():
    with pytest.raises(QueryError, match="Deals"):
        get_sheet(PARSED, "Nope")


def test_columns_resolve_case_insensitively():
    assert require_column(SHEET, "AMOUNT") == "amount"


def test_an_unknown_column_lists_the_available_ones():
    with pytest.raises(QueryError, match="deal_id"):
        require_column(SHEET, "totally_missing")


# ============================================================
# Filtering
# ============================================================


def test_equality_filter():
    rows = apply_filters(SHEET, [Filter("status", "eq", "contracted")])

    assert [row["deal_id"] for row in rows] == ["D-1", "D-2"]


def test_numeric_comparison_filters():
    rows = apply_filters(SHEET, [Filter("amount", "gte", 1000)])

    assert [row["deal_id"] for row in rows] == ["D-1", "D-2"]


def test_filters_combine_with_and():
    rows = apply_filters(
        SHEET,
        [Filter("status", "eq", "contracted"), Filter("amount", "lt", 2000)],
    )

    assert [row["deal_id"] for row in rows] == ["D-1"]


def test_contains_is_case_insensitive():
    rows = apply_filters(SHEET, [Filter("owner", "contains", "AM")])

    assert len(rows) == 2


def test_null_filters():
    assert len(apply_filters(SHEET, [Filter("owner", "is_null", None)])) == 1
    assert len(apply_filters(SHEET, [Filter("owner", "is_not_null", None)])) == 3


def test_null_never_matches_a_comparison():
    """SQL semantics: the row with amount=None is excluded either way."""

    assert len(apply_filters(SHEET, [Filter("amount", "gte", 0)])) == 3
    assert len(apply_filters(SHEET, [Filter("amount", "lt", 0)])) == 0


def test_numbers_stored_as_text_still_compare_numerically():
    """
    [claude] Every CSV column arrives as text. Comparing "90" against 1200 as
    strings would put 90 above it, so both sides are coerced to numbers when
    both can be.
    """

    sheet = SheetContent(
        name="S",
        columns=(ColumnSpec("amount", "numeric_text", 2),),
        rows=({"amount": "90"}, {"amount": "1200"}),
    )

    rows = apply_filters(sheet, [Filter("amount", "gt", 100)])

    assert [row["amount"] for row in rows] == ["1200"]


def test_an_unknown_operator_is_rejected():
    with pytest.raises(QueryError, match="Unknown operator"):
        apply_filters(SHEET, [Filter("status", "regex", "x")])


# ============================================================
# Aggregation
# ============================================================


def test_count_covers_every_matching_row():
    result = aggregate(SHEET, "count")

    assert result["value"] == 4


def test_sum_is_computed_over_all_rows_not_a_sample():
    result = aggregate(SHEET, "sum", column="amount")

    assert result["value"] == 3900  # 1000 + 2500 + 400
    assert result["matched_rows"] == 4


def test_average_divides_by_rows_with_values_not_by_all_rows():
    """
    [claude] The denominator question, on the file side. Three rows carry an
    amount; the fourth is empty. Dividing 3900 by 4 gives 975 and is wrong.
    """

    result = aggregate(SHEET, "avg", column="amount")

    assert result["value"] == pytest.approx(1300.0)


def test_filters_apply_before_aggregation():
    result = aggregate(
        SHEET,
        "sum",
        column="amount",
        filters=[Filter("status", "eq", "contracted")],
    )

    assert result["value"] == 3500
    assert result["matched_rows"] == 2


def test_group_by_splits_the_total():
    result = aggregate(SHEET, "sum", column="amount", group_by="status")

    assert result["groups"]["contracted"]["value"] == 3500
    assert result["groups"]["eoi"]["value"] == 400


def test_group_by_names_blank_keys_rather_than_dropping_them():
    result = aggregate(SHEET, "count", group_by="owner")

    assert result["groups"]["(blank)"]["rows"] == 1


def test_non_numeric_values_are_reported_not_treated_as_zero():
    """
    A column holding "N/A" among its numbers produces an average that looks
    right and is not, unless the skipped count is surfaced.
    """

    sheet = SheetContent(
        name="S",
        columns=(ColumnSpec("amount", "text", 3),),
        rows=({"amount": "100"}, {"amount": "N/A"}, {"amount": "200"}),
    )

    result = aggregate(sheet, "avg", column="amount")

    assert result["value"] == pytest.approx(150.0)
    assert result["non_numeric_skipped"] == 1


def test_min_and_max():
    assert aggregate(SHEET, "min", column="amount")["value"] == 400
    assert aggregate(SHEET, "max", column="amount")["value"] == 2500


def test_an_aggregate_over_nothing_returns_none_rather_than_zero():
    """Zero is a number someone will report. None is not."""

    result = aggregate(
        SHEET,
        "sum",
        column="amount",
        filters=[Filter("status", "eq", "cancelled")],
    )

    assert result["value"] is None
    assert result["matched_rows"] == 0


def test_an_unknown_aggregation_is_rejected():
    with pytest.raises(QueryError, match="Unknown aggregation"):
        aggregate(SHEET, "median", column="amount")


def test_sum_without_a_column_is_rejected():
    with pytest.raises(QueryError, match="needs a column"):
        aggregate(SHEET, "sum")
