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

    Does not retrieve CRM data.
    """

    return {
        "success": True,
        "first": first,
        "second": second,
        "difference": first - second,
    }


@tool
def calculate_pipeline_distribution(
    counts: dict[str, int],
) -> dict[str, Any]:
    """
    Calculate the percentage distribution of supplied deal counts.

    Example input:
        {
            "eoi": 40,
            "reservation": 30,
            "contracted": 30
        }

    Does not retrieve CRM data.
    """

    total = sum(counts.values())

    if total == 0:
        return {
            "success": False,
            "error": "Cannot calculate distribution from zero total deals.",
        }

    distribution = {
        key: round((value / total) * 100, 2)
        for key, value in counts.items()
    }

    return {
        "success": True,
        "total": total,
        "counts": counts,
        "distribution": distribution,
    }


@tool
def analyze_deal_aging(
    deals: list[dict[str, Any]],
    aging_threshold_days: int = 30,
) -> dict[str, Any]:
    """
    Analyze deal age using already-retrieved deal records.

    Each deal should contain:
        id
        days_in_stage

    This tool does not retrieve CRM data.
    """

    if aging_threshold_days < 0:
        return {
            "success": False,
            "error": "aging_threshold_days cannot be negative.",
        }

    if not deals:
        return {
            "success": True,
            "total_deals": 0,
            "stale_deals": [],
            "stale_count": 0,
        }

    stale_deals = []

    for deal in deals:
        days = deal.get("days_in_stage")

        if days is None:
            continue

        if days >= aging_threshold_days:
            stale_deals.append(
                {
                    "id": deal.get("id"),
                    "days_in_stage": days,
                    "deal": deal,
                }
            )

    return {
        "success": True,
        "total_deals": len(deals),
        "stale_count": len(stale_deals),
        "stale_deals": stale_deals,
        "aging_threshold_days": aging_threshold_days,
    }


@tool
def identify_stale_deals(
    deals: list[dict[str, Any]],
    threshold_days: int = 30,
) -> dict[str, Any]:
    """
    Identify deals whose days_in_stage meets or exceeds the supplied
    threshold.

    This operates only on deal records already retrieved by the SQL tool.
    """

    if threshold_days < 0:
        return {
            "success": False,
            "error": "threshold_days cannot be negative.",
        }

    stale_deals = []

    for deal in deals:
        days = deal.get("days_in_stage")

        if days is None:
            continue

        if days >= threshold_days:
            stale_deals.append(deal)

    return {
        "success": True,
        "threshold_days": threshold_days,
        "stale_count": len(stale_deals),
        "stale_deals": stale_deals,
    }


@tool
def rank_deals(
    deals: list[dict[str, Any]],
    field: str,
    descending: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Rank already-retrieved deals by a numeric field.

    Example:
        field="area"
        descending=True
        limit=5

    Does not retrieve CRM data.
    """

    if limit <= 0:
        return {
            "success": False,
            "error": "limit must be greater than zero.",
        }

    valid_deals = []

    for deal in deals:
        value = deal.get(field)

        if isinstance(value, (int, float)):
            valid_deals.append(deal)

    ranked = sorted(
        valid_deals,
        key=lambda deal: deal[field],
        reverse=descending,
    )

    return {
        "success": True,
        "field": field,
        "descending": descending,
        "limit": limit,
        "total_available": len(ranked),
        "deals": ranked[:limit],
    }


@tool
def compare_periods(
    current: dict[str, float],
    previous: dict[str, float],
) -> dict[str, Any]:
    """
    Compare matching metrics from two already-retrieved periods.

    Example:
        current={"deals": 150, "reservations": 40}
        previous={"deals": 120, "reservations": 32}

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
def build_deal_summary(
    deal: dict[str, Any],
) -> dict[str, Any]:
    """
    Structure an already-retrieved deal into a concise summary.

    This tool does not retrieve CRM data.
    """

    return {
        "success": True,
        "summary": {
            "id": deal.get("id"),
            "client": deal.get("client_name"),
            "status": deal.get("status"),
            "unit": deal.get("unit_number"),
            "project_id": deal.get("project_id"),
            "area": deal.get("area"),
            "selling_type": deal.get("selling_type"),
            "reservation_date": deal.get("reservation_date"),
            "contract_date": deal.get("contract_date"),
            "expected_closing_date": deal.get("expected_closing_date"),
            "days_in_stage": deal.get("days_in_stage"),
        },
    }


DEALS_TOOLS = [
    calculate_percentage,
    calculate_percentage_change,
    calculate_average,
    calculate_difference,
    calculate_pipeline_distribution,
    analyze_deal_aging,
    identify_stale_deals,
    rank_deals,
    compare_periods,
    build_deal_summary,
]


__all__ = [
    "calculate_percentage",
    "calculate_percentage_change",
    "calculate_average",
    "calculate_difference",
    "calculate_pipeline_distribution",
    "analyze_deal_aging",
    "identify_stale_deals",
    "rank_deals",
    "compare_periods",
    "build_deal_summary",
    "DEALS_TOOLS",
]