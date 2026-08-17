"""
[claude] Leads-domain analysis tools.

Like the shared analysis tools, these operate only on numbers the agent
already holds. They never retrieve CRM data — `sql_query` is the only path
to the database, and pushing rows back through the model to be counted is
the mistake these tools exist to avoid, not repeat.

Only one tool lives here. Everything else a leads question needs is either
plain SQL (filtering, grouping, counting) or already in the shared analysis
set. A funnel is the exception: it is a sequence of dependent ratios, and
expressing it as a chain of separate percentage calls is both clumsy and
easy for the model to get wrong — step conversion and conversion from the
top of the funnel are different numbers, and they get conflated.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool


@tool
def calculate_funnel(
    stages: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Analyse an ordered funnel of stage counts.

    `stages` must be ordered from the top of the funnel downwards, each entry
    being {"stage": <name or id>, "count": <number>}. For example:

        [{"stage": "New Lead", "count": 1200},
         {"stage": "Potential", "count": 480},
         {"stage": "Meeting Done", "count": 160}]

    Returns, for every stage: conversion from the previous stage, conversion
    from the top of the funnel, and how many were lost at that step. Use it
    after a GROUP BY query that returns counts per stage.

    Operates only on values supplied by the agent.
    Does not retrieve CRM data.
    """

    if not stages:
        return {
            "success": False,
            "error": "Cannot analyse a funnel with no stages.",
        }

    parsed: list[tuple[Any, float]] = []

    for position, entry in enumerate(stages):
        if not isinstance(entry, dict) or "count" not in entry:
            return {
                "success": False,
                "error": (
                    f"Stage at position {position} must be an object with "
                    '"stage" and "count".'
                ),
            }

        try:
            count = float(entry["count"])
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": (
                    f"Stage '{entry.get('stage', position)}' has a "
                    "non-numeric count."
                ),
            }

        if count < 0:
            return {
                "success": False,
                "error": (
                    f"Stage '{entry.get('stage', position)}' has a negative "
                    "count."
                ),
            }

        parsed.append((entry.get("stage", position), count))

    top_count = parsed[0][1]

    if top_count == 0:
        return {
            "success": False,
            "error": "The top of the funnel is zero, so no rate is defined.",
        }

    steps = []
    warnings: list[str] = []
    previous_count = None

    for stage, count in parsed:
        # First stage has no predecessor, so step conversion is undefined
        # rather than 100% — reporting 100% would imply a measured step.
        if previous_count is None:
            from_previous = None
            dropped = None
        elif previous_count == 0:
            from_previous = None
            dropped = None
        else:
            from_previous = round((count / previous_count) * 100, 2)
            dropped = previous_count - count

            # [claude] More leads left a stage than entered it, which cannot
            # happen in a real funnel. It means the stages were given in the
            # wrong order — almost always because ascending stage id was
            # assumed to be funnel order, which the CRM does not expose.
            #
            # The prompt already warns that a conversion above 100% is the
            # giveaway, but that left the whole defence resting on the model
            # noticing a number it just produced. Flagging it here means the
            # signal survives a prompt edit.
            if from_previous > 100:
                warnings.append(
                    f"'{stage}' holds more than the stage before it "
                    f"({from_previous}% conversion, which is above 100%). "
                    "These stages are not in funnel order — do not report "
                    "this as a funnel."
                )

        steps.append(
            {
                "stage": stage,
                "count": count,
                "from_previous_percent": from_previous,
                "from_top_percent": round((count / top_count) * 100, 2),
                "dropped_from_previous": dropped,
            }
        )
        previous_count = count

    bottom_count = parsed[-1][1]

    result = {
        "success": True,
        "steps": steps,
        "top_stage": parsed[0][0],
        "bottom_stage": parsed[-1][0],
        "overall_conversion_percent": round((bottom_count / top_count) * 100, 2),
        "total_lost": top_count - bottom_count,
    }

    if warnings:
        result["warnings"] = warnings

    return result


LEADS_TOOLS = [
    calculate_funnel,
]


__all__ = [
    "LEADS_TOOLS",
    "calculate_funnel",
]
