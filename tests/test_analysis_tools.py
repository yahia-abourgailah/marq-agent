import pytest

from app.tools.analysis import (
    calculate_average,
    calculate_difference,
    calculate_percentage,
    calculate_percentage_change,
    calculate_share,
    compare_periods,
)


def test_percentage():
    result = calculate_percentage.invoke({
        "part": 25,
        "total": 100,
    })

    assert result["percentage"] == 25


def test_percentage_change():
    result = calculate_percentage_change.invoke({
        "old_value": 100,
        "new_value": 120,
    })

    assert result["percentage_change"] == 20


def test_average():
    result = calculate_average.invoke({
        "values": [10, 20, 30],
    })

    assert result["average"] == 20


def test_difference():
    result = calculate_difference.invoke({
        "first": 150,
        "second": 100,
    })

    assert result["difference"] == 50


def test_compare_periods():
    result = compare_periods.invoke({
        "current": {
            "deals": 150,
            "reservations": 40,
        },
        "previous": {
            "deals": 120,
            "reservations": 32,
        },
    })

    assert result["success"] is True
    assert result["comparison"]["deals"]["difference"] == 30
    assert result["comparison"]["deals"]["percentage_change"] == 25


# ============================================================
# [claude] calculate_share was entirely untested — 0 of its lines covered.
#
# It is the tool at the centre of the denominator bug family: variant 1 in
# docs/HANDOFF.md is "subset percentage via calculate_share — summed 225 +
# 245 to 470". The tool's own docstring warns against that use, and nothing
# checked that the arithmetic underneath was right.
# ============================================================


def test_share_splits_buckets_by_their_combined_total():
    result = calculate_share.func(
        {"eoi": 15, "reservation": 5, "contracted": 225}
    )

    assert result["success"] is True
    assert result["total"] == 245

    shares = result["shares"]
    assert shares["contracted"]["share_percent"] == pytest.approx(91.84)
    assert shares["eoi"]["share_percent"] == pytest.approx(6.12)
    assert shares["reservation"]["share_percent"] == pytest.approx(2.04)


def test_shares_sum_to_one_hundred():
    """The defining property: these are parts of one whole."""

    result = calculate_share.func({"a": 310, "b": 160, "c": 82, "d": 7})

    total = sum(s["share_percent"] for s in result["shares"].values())

    assert total == pytest.approx(100.0, abs=0.05)


def test_shares_are_ordered_largest_first():
    result = calculate_share.func({"small": 5, "big": 500, "middle": 50})

    assert list(result["shares"]) == ["big", "middle", "small"]


def test_share_reports_the_count_alongside_the_percentage():
    """A percentage without its count cannot be sanity-checked by a reader."""

    result = calculate_share.func({"a": 3, "b": 1})

    assert result["shares"]["a"]["count"] == 3
    assert result["shares"]["b"]["count"] == 1


def test_share_of_nothing_is_refused():
    assert calculate_share.func({})["success"] is False


def test_share_of_all_zeros_is_refused_not_divided_by_zero():
    result = calculate_share.func({"a": 0, "b": 0})

    assert result["success"] is False
    assert "zero" in result["error"].lower()


def test_a_single_bucket_is_the_whole():
    result = calculate_share.func({"only": 42})

    assert result["shares"]["only"]["share_percent"] == 100.0


def test_a_zero_bucket_among_others_is_kept_at_zero_percent():
    """Dropping it would make the reader think the bucket does not exist."""

    result = calculate_share.func({"a": 10, "empty": 0})

    assert result["shares"]["empty"]["share_percent"] == 0.0
    assert result["shares"]["empty"]["count"] == 0


def test_share_summing_a_subset_and_its_total_is_arithmetically_wrong():
    """
    [claude] Denominator bug variant 1, pinned as a test rather than only a
    docstring warning. 225 contracted deals out of 245 active is 91.84%, but
    handing both to calculate_share sums them to 470 and reports 47.87% —
    a plausible-looking number that answers no question at all.

    The tool cannot detect this (it has no way to know one count contains
    the other), so this test documents the trap and proves the shape of the
    wrong answer, which is what makes it recognisable in a trace.
    """

    misuse = calculate_share.func({"contracted": 225, "total_active": 245})

    assert misuse["total"] == 470
    assert misuse["shares"]["contracted"]["share_percent"] == pytest.approx(47.87)

    correct = calculate_percentage.func(part=225, total=245)

    assert correct["percentage"] == pytest.approx(91.84)


# ============================================================
# [claude] compare_periods branches that were never reached.
# ============================================================


def test_compare_periods_marks_a_metric_missing_from_one_side():
    """
    The branch that used to omit `difference` entirely, giving a metric a
    different shape depending on whether both periods had it — the model
    reads these by key and would hit a missing one on exactly the
    comparisons most worth flagging.
    """

    result = compare_periods.func(
        current={"deals": 10, "new_metric": 5},
        previous={"deals": 8},
    )

    new_metric = result["comparison"]["new_metric"]

    assert new_metric["current"] == 5
    assert new_metric["previous"] is None
    assert new_metric["difference"] is None
    assert new_metric["percentage_change"] is None

    # And the metric present in both keeps the full shape.
    assert result["comparison"]["deals"]["difference"] == 2


def test_compare_periods_handles_growth_from_zero_without_dividing():
    result = compare_periods.func(current={"deals": 12}, previous={"deals": 0})

    entry = result["comparison"]["deals"]

    assert entry["difference"] == 12
    assert entry["percentage_change"] is None


def test_compare_periods_reports_a_decline_as_negative():
    result = compare_periods.func(current={"deals": 75}, previous={"deals": 100})

    entry = result["comparison"]["deals"]

    assert entry["difference"] == -25
    assert entry["percentage_change"] == pytest.approx(-25.0)


def test_compare_periods_over_two_empty_periods_is_empty_not_an_error():
    result = compare_periods.func(current={}, previous={})

    assert result["success"] is True
    assert result["comparison"] == {}


# ============================================================
# [claude] Guard branches on the simple tools.
# ============================================================


def test_percentage_of_zero_total_is_refused():
    assert calculate_percentage.func(part=5, total=0)["success"] is False


def test_percentage_change_from_zero_is_refused():
    result = calculate_percentage_change.func(old_value=0, new_value=10)

    assert result["success"] is False


def test_average_of_nothing_is_refused_not_zero():
    """Zero is a number someone will report as an average."""

    assert calculate_average.func(values=[])["success"] is False
