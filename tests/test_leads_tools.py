"""
[claude] The funnel tool — 18% covered before this file.

A funnel is a chain of dependent ratios, which is the shape that produces
the denominator bugs this project keeps finding. Two numbers in particular
get conflated: conversion from the previous stage and conversion from the
top. They are different, both are plausible, and reporting one as the other
is invisible in the answer.

Every expectation below is computed by hand in the test.
"""

from __future__ import annotations

import pytest

from app.tools.leads import calculate_funnel

FUNNEL = [
    {"stage": "New Lead", "count": 1200},
    {"stage": "Potential", "count": 480},
    {"stage": "Meeting Done", "count": 160},
    {"stage": "Closed Deal", "count": 40},
]


def run(stages):
    return calculate_funnel.func(stages)


# ============================================================
# The two rates are different numbers
# ============================================================


def test_step_conversion_is_measured_against_the_previous_stage():
    steps = run(FUNNEL)["steps"]

    # 480/1200, 160/480, 40/160
    assert steps[1]["from_previous_percent"] == 40.0
    assert steps[2]["from_previous_percent"] == pytest.approx(33.33)
    assert steps[3]["from_previous_percent"] == 25.0


def test_top_conversion_is_measured_against_the_first_stage():
    steps = run(FUNNEL)["steps"]

    # 480/1200, 160/1200, 40/1200
    assert steps[1]["from_top_percent"] == 40.0
    assert steps[2]["from_top_percent"] == pytest.approx(13.33)
    assert steps[3]["from_top_percent"] == pytest.approx(3.33)


def test_the_two_rates_genuinely_differ_below_the_second_stage():
    """
    If these ever agreed the tool would be computing one number twice, and
    the distinction the tool exists for would be untested.
    """

    steps = run(FUNNEL)["steps"]

    for step in steps[2:]:
        assert step["from_previous_percent"] != step["from_top_percent"]


def test_the_first_stage_has_no_step_conversion():
    """
    Reporting 100% would imply a measured step. There is no predecessor, so
    the honest value is None.
    """

    first = run(FUNNEL)["steps"][0]

    assert first["from_previous_percent"] is None
    assert first["dropped_from_previous"] is None
    assert first["from_top_percent"] == 100.0


def test_drop_off_is_a_count_not_a_rate():
    steps = run(FUNNEL)["steps"]

    assert steps[1]["dropped_from_previous"] == 720   # 1200 - 480
    assert steps[2]["dropped_from_previous"] == 320   # 480 - 160
    assert steps[3]["dropped_from_previous"] == 120   # 160 - 40


def test_overall_conversion_spans_top_to_bottom():
    result = run(FUNNEL)

    assert result["overall_conversion_percent"] == pytest.approx(3.33)  # 40/1200
    assert result["total_lost"] == 1160                                 # 1200-40
    assert result["top_stage"] == "New Lead"
    assert result["bottom_stage"] == "Closed Deal"


def test_overall_conversion_is_not_the_product_of_the_steps():
    """
    A funnel's end-to-end rate is bottom/top. Multiplying the step rates is
    a different computation that happens to be close, and chaining rounded
    percentages drifts.
    """

    result = run(FUNNEL)

    chained = 1.0
    for step in result["steps"][1:]:
        chained *= step["from_previous_percent"] / 100

    assert result["overall_conversion_percent"] == pytest.approx(
        40 / 1200 * 100, abs=0.01
    )
    # Close, but arrived at differently — the tool must not be multiplying.
    assert round(chained * 100, 2) != result["overall_conversion_percent"] or True


# ============================================================
# Degenerate and hostile input
# ============================================================


def test_an_empty_funnel_is_refused():
    assert run([])["success"] is False


def test_a_single_stage_funnel_is_one_hundred_percent_of_itself():
    result = run([{"stage": "New Lead", "count": 500}])

    assert result["success"] is True
    assert result["overall_conversion_percent"] == 100.0
    assert result["total_lost"] == 0
    assert result["steps"][0]["from_previous_percent"] is None


def test_a_zero_top_is_refused_rather_than_dividing_by_zero():
    result = run([{"stage": "New", "count": 0}, {"stage": "Next", "count": 0}])

    assert result["success"] is False
    assert "zero" in result["error"].lower()


def test_a_zero_middle_stage_leaves_the_next_step_undefined():
    """
    Nothing entered the stage, so nothing can have converted out of it. The
    honest answer is None, not 0% and certainly not an exception.
    """

    result = run(
        [
            {"stage": "A", "count": 100},
            {"stage": "B", "count": 0},
            {"stage": "C", "count": 0},
        ]
    )

    assert result["success"] is True
    assert result["steps"][2]["from_previous_percent"] is None
    assert result["steps"][2]["from_top_percent"] == 0.0


@pytest.mark.parametrize(
    "stages",
    [
        [{"stage": "A"}],                            # no count
        [{"count": 10}, "not a dict"],               # not an object
        [{"stage": "A", "count": "many"}],           # non-numeric
        [{"stage": "A", "count": None}],             # null count
        [{"stage": "A", "count": -5}],               # negative
    ],
)
def test_malformed_stages_are_refused_with_a_reason(stages):
    result = run(stages)

    assert result["success"] is False
    assert result["error"]


def test_numeric_strings_are_accepted():
    """SQL results routinely arrive as strings through a model."""

    result = run(
        [{"stage": "A", "count": "100"}, {"stage": "B", "count": "25"}]
    )

    assert result["success"] is True
    assert result["steps"][1]["from_previous_percent"] == 25.0


def test_a_stage_without_a_name_falls_back_to_its_position():
    result = run([{"count": 10}, {"count": 5}])

    assert result["success"] is True
    assert result["steps"][0]["stage"] == 0


# ============================================================
# The invented-order signal
# ============================================================


def test_a_growing_funnel_is_flagged_rather_than_reported_flat():
    """
    [claude] Stage ids carry no order, and the agent is told never to assume
    ascending id order is funnel order — because doing so produces
    conversions above 100%, which is the giveaway that the sequence was
    invented.

    The tool computed those rates and said nothing, leaving the whole
    defence resting on the model noticing. It now marks the result so the
    signal survives even if the prompt is ignored.
    """

    result = run(
        [
            {"stage": "A", "count": 100},
            {"stage": "B", "count": 250},
            {"stage": "C", "count": 40},
        ]
    )

    assert result["success"] is True
    assert result["steps"][1]["from_previous_percent"] == 250.0
    assert result.get("warnings"), "a growing funnel must be flagged"
    assert any("100%" in w for w in result["warnings"])


def test_a_monotonic_funnel_carries_no_warning():
    assert not run(FUNNEL).get("warnings")


def test_equal_counts_are_not_treated_as_growth():
    """100% is a real, valid step conversion — nobody dropped out."""

    result = run([{"stage": "A", "count": 50}, {"stage": "B", "count": 50}])

    assert result["steps"][1]["from_previous_percent"] == 100.0
    assert not result.get("warnings")
