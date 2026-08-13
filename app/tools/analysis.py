"""
Scalar analysis tools.

These operate only on values the agent already holds. They never retrieve CRM
data — that is `sql_query`'s job alone.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool


@tool
def calculate_percentage(
    part: float,
    total: float,
) -> dict[str, Any]:
    """
    Calculate what percentage `part` represents of `total`.

    Operates only on values supplied by the agent.
    Does not retrieve CRM data.
    """

    if total == 0:
        return {
            "success": False,
            "error": "Cannot calculate percentage when total is zero.",
        }

    percentage = (part / total) * 100

    return {
        "success": True,
        "part": part,
        "total": total,
        "percentage": round(percentage, 2),
    }


@tool
def calculate_percentage_change(
    old_value: float,
    new_value: float,
) -> dict[str, Any]:
    """
    Calculate percentage change between two supplied values.

    Operates only on values supplied by the agent.
    Does not retrieve CRM data.
    """

    if old_value == 0:
        return {
            "success": False,
            "error": "Cannot calculate percentage change from zero.",
        }

    change = ((new_value - old_value) / old_value) * 100

    return {
        "success": True,
        "old_value": old_value,
        "new_value": new_value,
        "percentage_change": round(change, 2),
    }


@tool
def calculate_average(
    values: list[float],
) -> dict[str, Any]:
    """
    Calculate the average of supplied numeric values.

    Operates only on values supplied by the agent.
    Does not retrieve CRM data.
    """

    if not values:
        return {
            "success": False,
            "error": "Cannot calculate an average from an empty list.",
        }

    average = sum(values) / len(values)

    return {
        "success": True,
        "count": len(values),
        "average": round(average, 2),
    }


@tool
def calculate_difference(
    first: float,
    second: float,
) -> dict[str, Any]:
    """
    Calculate the difference between two supplied values.

    Operates only on values supplied by the agent.
    Does not retrieve CRM data.
    """

    return {
        "success": True,
        "first": first,
        "second": second,
        "difference": first - second,
    }


@tool
def compare_periods(
    current: dict[str, float],
    previous: dict[str, float],
) -> dict[str, Any]:
    """
    Compare matching metrics from two already-retrieved periods.

    Operates only on values supplied by the agent.
    Does not retrieve CRM data.
    """

    metrics = set(current) | set(previous)
    comparison = {}

    for metric in metrics:
        current_value = current.get(metric)
        previous_value = previous.get(metric)

        if current_value is None or previous_value is None:
            comparison[metric] = {
                "current": current_value,
                "previous": previous_value,
                # [claude] Added "difference": None. This branch used to omit
                # the key entirely, so a metric present in only one period came
                # back with a different shape than a metric present in both.
                # The model reads these dicts positionally by key and would hit
                # a missing key on exactly the comparisons most worth flagging.
                "difference": None,
                "percentage_change": None,
            }
            continue

        if previous_value == 0:
            percentage_change = None
        else:
            percentage_change = round(
                ((current_value - previous_value) / previous_value) * 100,
                2,
            )

        comparison[metric] = {
            "current": current_value,
            "previous": previous_value,
            "difference": current_value - previous_value,
            "percentage_change": percentage_change,
        }

    return {
        "success": True,
        "comparison": comparison,
    }


@tool
def calculate_share(
    counts: dict[str, float],
) -> dict[str, Any]:
    """
    Split mutually exclusive bucket counts into each bucket's share of their
    combined total.

    Use this ONLY for buckets that do not overlap and together make up the
    whole — the output of a GROUP BY, such as leads by utm_source or deals by
    status. The total is computed by summing the counts you pass in.

    Do NOT use it when one number is a subset of another. "225 contracted out
    of 245 active" is not two buckets: contracted deals are part of the active
    deals, so summing them gives 470, which is meaningless. Use
    calculate_percentage(part=225, total=245) for that.

        buckets     {"eoi": 15, "reservation": 5, "contracted": 225}   yes
        subset      {"contracted": 225, "total_active": 245}           no

    Operates only on values supplied by the agent.
    Does not retrieve CRM data.
    """

    if not counts:
        return {
            "success": False,
            "error": "Cannot calculate shares from an empty set of counts.",
        }

    total = sum(counts.values())

    if total == 0:
        return {
            "success": False,
            "error": "Cannot calculate shares when the total is zero.",
        }

    shares = {
        bucket: {
            "count": value,
            "share_percent": round((value / total) * 100, 2),
        }
        for bucket, value in counts.items()
    }

    return {
        "success": True,
        "total": total,
        "shares": dict(
            sorted(
                shares.items(),
                key=lambda item: item[1]["count"],
                reverse=True,
            )
        ),
    }


ANALYSIS_TOOLS = [
    calculate_percentage,
    calculate_percentage_change,
    calculate_average,
    calculate_difference,
    compare_periods,
    calculate_share,  # [claude]
]


__all__ = [
    "calculate_percentage",
    "calculate_percentage_change",
    "calculate_average",
    "calculate_difference",
    "compare_periods",
    "calculate_share",  # [claude]
    "ANALYSIS_TOOLS",
]
