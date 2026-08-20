"""
[claude] The charting tool.

A chart is more persuasive than a sentence, which cuts both ways: the
failure this project keeps finding is a confident wrong number, and a wrong
number drawn as a bar is worse than the same number in prose, because a
reader checks a sentence and trusts a picture.

So the tool validates rather than trusts. A spec that would render
misleadingly — mismatched lengths, a donut of negatives, more categories
than a chart can carry — is refused, because a chart that renders wrong is
much harder to spot than one that does not render.
"""

from __future__ import annotations

import pytest

from app.tools.charts import (
    CHART_TOOLS,
    MAX_DONUT_SLICES,
    MAX_POINTS,
    collect,
)

[make_chart] = CHART_TOOLS


def draw(**kwargs):
    base = {
        "title": "Deals by status",
        "kind": "column",
        "labels": ["contracted", "cancelled"],
        "values": [225, 70],
    }

    return make_chart.invoke({**base, **kwargs})


# ============================================================
# What it accepts
# ============================================================


def test_a_valid_chart_is_collected_with_its_values_intact():
    with collect() as drawn:
        result = draw(series_name="Deals")

    assert result["success"] is True

    spec = drawn.charts[0]

    assert spec["title"] == "Deals by status"
    assert spec["kind"] == "column"
    assert spec["labels"] == ["contracted", "cancelled"]
    assert spec["series"][0]["values"] == [225.0, 70.0]


def test_nothing_is_collected_when_nobody_is_collecting():
    """
    The default outside an HTTP request — the evals and the CLI run the same
    tool and must not accumulate specs nobody reads.
    """

    assert draw()["success"] is True  # must not raise


@pytest.mark.parametrize("kind", ["bar", "column", "line", "donut"])
def test_every_documented_kind_is_accepted(kind):
    with collect() as drawn:
        assert draw(kind=kind)["success"] is True

    assert drawn.charts[0]["kind"] == kind


def test_a_second_series_is_carried_for_comparison():
    with collect() as drawn:
        draw(
            series_name="Contracted",
            compare_values=[10, 7],
            compare_name="Cancelled",
        )

    series = drawn.charts[0]["series"]

    assert [s["name"] for s in series] == ["Contracted", "Cancelled"]
    assert series[1]["values"] == [10.0, 7.0]


# ============================================================
# What it refuses
# ============================================================


def refused(**kwargs):
    with collect() as drawn:
        result = draw(**kwargs)

    assert result["success"] is False, "should have been refused"
    assert drawn.charts == [], "a refused chart must not be collected"

    return result["error"]


def test_mismatched_labels_and_values_are_refused():
    """
    [claude] The most dangerous malformed spec, because it still draws.

    With three labels and two values a naive renderer shows two bars against
    three names — every category silently shifted by one, and the chart
    looks entirely normal.
    """

    error = refused(labels=["a", "b", "c"], values=[1, 2])

    assert "match one to one" in error


@pytest.mark.parametrize("bad", [[float("nan")], [float("inf")], [float("-inf")]])
def test_non_finite_values_are_refused_by_the_validator(bad):
    """
    NaN and infinity are valid Python floats, so the tool schema lets them
    through — and they draw as a bar of no height or one off the canvas.
    """

    assert "finite number" in refused(labels=["a"], values=bad)


@pytest.mark.parametrize("bad", [["not a number"], [None]])
def test_non_numeric_values_are_refused_by_the_tool_schema(bad):
    """
    [claude] Caught a layer earlier than the validator, and worth pinning
    where.

    `values: list[float]` means pydantic rejects these at the tool boundary
    before `make_chart` runs, so the agent gets a schema error rather than
    my message. Both refuse; asserting the wrong layer would make this test
    fail for a correct system, which is how a good implementation gets
    "fixed" into a worse one.
    """

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        draw(labels=["a"], values=bad)


def test_an_unknown_kind_is_refused():
    assert "kind must be one of" in refused(kind="pyramid")


def test_too_many_categories_are_refused_rather_than_truncated():
    """
    Silently dropping the tail would misrepresent the data — the chart would
    claim to show a breakdown while hiding part of it.
    """

    n = MAX_POINTS + 3
    error = refused(labels=[f"c{i}" for i in range(n)], values=list(range(n)))

    assert "too many to read" in error
    assert "Other" in error


def test_a_donut_with_too_many_slices_is_refused():
    n = MAX_DONUT_SLICES + 1
    error = refused(
        kind="donut", labels=[f"s{i}" for i in range(n)], values=[1] * n
    )

    assert "unreadable" in error


def test_a_donut_cannot_show_negative_values():
    """A share of a whole has no negative slice; the arc would be nonsense."""

    assert "negative" in refused(kind="donut", labels=["a", "b"], values=[5, -2])


def test_a_donut_cannot_take_a_second_series():
    assert "one series" in refused(
        kind="donut", compare_values=[1, 2], compare_name="Other"
    )


def test_a_comparison_without_a_name_is_refused():
    """
    Two unnamed series cannot be told apart in the legend, which is the
    only dependable identity channel a chart has.
    """

    assert "compare_name is required" in refused(compare_values=[1, 2])


def test_a_comparison_of_the_wrong_length_is_refused():
    assert "one number per label" in refused(
        compare_values=[1], compare_name="Cancelled"
    )


def test_an_empty_title_is_refused():
    """The title is what a single-series chart uses instead of a legend."""

    assert "title is required" in refused(title="   ")


def test_a_refusal_is_retryable():
    """
    Unlike a masked column, every one of these is fixed by a corrected
    call, so the agent should try again rather than give up.
    """

    with collect():
        result = draw(labels=["a", "b", "c"], values=[1])

    assert result["retryable"] is True
