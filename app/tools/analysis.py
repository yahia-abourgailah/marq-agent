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


ANALYSIS_TOOLS = [
    calculate_percentage,
    calculate_percentage_change,
    calculate_average,
    calculate_difference,
    compare_periods,
]


__all__ = [
    "calculate_percentage",
    "calculate_percentage_change",
    "calculate_average",
    "calculate_difference",
    "compare_periods",
    "ANALYSIS_TOOLS",
]
