"""
[claude] Charts, when the user asks for one.

A chart is more persuasive than a sentence, which cuts both ways. The failure
this project keeps finding is a confident wrong number, and a wrong number
drawn as a bar is worse than the same number in prose: the reader checks a
sentence and trusts a picture.

Three things follow from that, and they are the design:

*   **The values are labelled on the marks and listed in a table.** Nothing is
    only visible as a length. A reader can compare what the chart shows
    against what the answer says without squinting at pixels.

*   **The chart sits beside the query that produced it.** `provenance` already
    carries the SQL; the chart is drawn from figures the agent read out of
    that result, so both are in the same response and both are inspectable.

*   **The spec is validated here, not trusted.** Mismatched lengths, empty
    series, non-finite values and absurd category counts are refused rather
    than drawn, because a chart that renders wrong is harder to spot than one
    that does not render.

Rendering happens in the browser from a small JSON spec. No matplotlib, no
image bytes through the model, and — the point — no chance of the model
describing a picture it cannot see. It states the numbers; the client draws
them.
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger("marq.charts")

KINDS = ("bar", "column", "line", "donut")

# [claude] Above this a chart stops being readable and starts being a
# decorated table — the bars get thinner than their own labels. The rule the
# dataviz guidance gives is to fold the tail into "Other" or facet; either
# way that is the agent's decision to make before calling, so this refuses
# rather than silently truncating.
MAX_POINTS = 12

# A donut divides one whole. More than six slices is a bar chart wearing a
# circle, and the small slices become unreadable.
MAX_DONUT_SLICES = 6

_COLLECTOR: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "charts", default=None
)


@dataclass
class Collector:
    charts: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def collect():
    """Collect every chart built inside this block."""

    collector = Collector()
    token = _COLLECTOR.set(collector.charts)

    try:
        yield collector
    finally:
        _COLLECTOR.reset(token)


def record(spec: dict[str, Any]) -> None:
    """Note one chart, if anything is collecting. A no-op otherwise."""

    charts = _COLLECTOR.get()

    if charts is None:
        return

    charts.append(spec)


def _numeric(values: list[Any]) -> list[float] | None:
    """Every value as a finite float, or None if any is not."""

    cleaned: list[float] = []

    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(number):
            return None

        cleaned.append(number)

    return cleaned


def build_chart_tools():
    """Build the charting tools. Holds no state and reaches no service."""

    @tool
    def make_chart(
        title: str,
        kind: str,
        labels: list[str],
        values: list[float],
        series_name: str | None = None,
        compare_values: list[float] | None = None,
        compare_name: str | None = None,
        value_suffix: str | None = None,
    ) -> dict:
        """
        Draw a chart of figures you have already retrieved.

        Call this only when the user asks to see a chart, graph, plot or
        visual — or asks for a breakdown that is plainly easier to read as
        one. Never chart a single number; say it.

        Use figures you got from `sql_query` or a workspace tool in THIS
        turn. Do not estimate, round for neatness, or reuse numbers from
        memory: the chart and your written answer must agree exactly,
        because a reader will compare them.

        Args:
            title: What the chart shows, in plain words. "Deals by status".
            kind: One of bar (horizontal, best for long category names),
                column (vertical, best for time or few categories),
                line (a trend over time), donut (share of one whole).
            labels: The category or time labels, in the order to display.
            values: One number per label, same length and order.
            series_name: What the values measure — "Deals", "Leads".
                Shown on the axis and in the table.
            compare_values: A second series for a side-by-side comparison,
                same length as labels. Omit unless comparing two measures.
            compare_name: What that second series measures. Required with
                compare_values.
            value_suffix: A unit to show after each value — "%", " sqm".

        Returns a confirmation. The chart is delivered to the user's screen
        separately; do not describe it as an image or claim to see it. Still
        state the key figures in your written answer.
        """

        problems: list[str] = []

        kind_clean = (kind or "").strip().lower()

        if kind_clean not in KINDS:
            problems.append(f"kind must be one of {', '.join(KINDS)}")

        if not title or not title.strip():
            problems.append("title is required")

        if not labels:
            problems.append("labels cannot be empty")

        numbers = _numeric(values or [])

        if numbers is None:
            problems.append("every value must be a finite number")
        elif labels and len(labels) != len(numbers):
            problems.append(
                f"{len(labels)} labels but {len(numbers)} values — "
                "they must match one to one"
            )

        if labels and len(labels) > MAX_POINTS:
            problems.append(
                f"{len(labels)} categories is too many to read; show the top "
                f"{MAX_POINTS} or group the rest as 'Other'"
            )

        if kind_clean == "donut" and labels and len(labels) > MAX_DONUT_SLICES:
            problems.append(
                f"a donut with {len(labels)} slices is unreadable; use "
                f"kind='bar', or keep to {MAX_DONUT_SLICES} slices"
            )

        second: list[float] | None = None

        if compare_values is not None:
            second = _numeric(compare_values)

            if second is None:
                problems.append("every compare_value must be a finite number")
            elif labels and len(second) != len(labels):
                problems.append(
                    "compare_values must have one number per label"
                )

            if not compare_name or not compare_name.strip():
                problems.append("compare_name is required with compare_values")

            if kind_clean == "donut":
                problems.append("a donut shows one series; drop compare_values")

        if kind_clean == "donut" and numbers and any(n < 0 for n in numbers):
            problems.append("a donut cannot show negative values")

        if problems:
            # [claude] Non-retryable would be wrong here: unlike a masked
            # column, these are all things a corrected call fixes.
            return {
                "success": False,
                "retryable": True,
                "error": "; ".join(problems),
            }

        series = [
            {"name": (series_name or "Value").strip(), "values": numbers}
        ]

        if second is not None:
            series.append({"name": compare_name.strip(), "values": second})

        spec = {
            "title": title.strip(),
            "kind": kind_clean,
            "labels": [str(label) for label in labels],
            "series": series,
            "value_suffix": (value_suffix or "").strip() or None,
        }

        # Out of band, exactly as provenance is — the model wrote these
        # numbers and does not need them read back.
        record(spec)

        logger.info(
            "chart_made",
            extra={"kind": kind_clean, "points": len(labels)},
        )

        return {
            "success": True,
            "rendered": True,
            "kind": kind_clean,
            "points": len(labels),
            "note": (
                "The chart is on the user's screen. Still state the key "
                "figures in your answer — do not say 'as shown above' and "
                "leave the numbers only in the picture."
            ),
        }

    return [make_chart]


CHART_TOOLS = tuple(build_chart_tools())


__all__ = [
    "CHART_TOOLS",
    "KINDS",
    "MAX_DONUT_SLICES",
    "MAX_POINTS",
    "Collector",
    "build_chart_tools",
    "collect",
    "record",
]
