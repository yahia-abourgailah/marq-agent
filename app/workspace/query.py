"""
[claude] Exact operations over parsed spreadsheet content.

This module is the counterweight to retrieval. Semantic search finds the part
of a file worth looking at; these functions compute over *all* of it. Every
number the agent reports about a spreadsheet comes from here.

Why not let the agent write code, or SQL, over the sheet
--------------------------------------------------------
Because then the sheet needs a guard, and the project already has one guard
whose entire design — an allowlist derived from a catalogue, so the surface
described and the surface permitted cannot drift — depends on knowing the
schema ahead of time. An uploaded file has no schema until it arrives, so
that guarantee cannot be reproduced for it.

A small fixed vocabulary of operations avoids the question. Filters are
data, not expressions: there is no string that gets parsed and evaluated,
so there is nothing for a malicious spreadsheet to inject into.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.workspace.models import ParsedFile, SheetContent

# Comparison operators a filter may use.
OPERATORS = (
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "is_null",
    "is_not_null",
)

AGGREGATIONS = ("count", "sum", "avg", "min", "max")

# Ceiling on rows returned by an explicit read, mirroring the SQL side's
# MAX_ROWS. Aggregates are unaffected — they run over every row.
MAX_RETURNED_ROWS = 200


class QueryError(ValueError):
    """Raised when a query refers to something the sheet does not have."""


@dataclass(frozen=True)
class Filter:
    column: str
    op: str
    value: Any = None


def get_sheet(parsed: ParsedFile, sheet_name: str | None) -> SheetContent:
    """
    Resolve a worksheet by name, defaulting to the only one.

    [claude] Raises rather than guessing when the name is wrong. A silent
    fallback to the first sheet answers a question about the wrong data and
    looks exactly like a correct answer.
    """

    if not parsed.sheets:
        raise QueryError("This file has no worksheets.")

    if sheet_name is None:
        if len(parsed.sheets) == 1:
            return parsed.sheets[0]

        names = ", ".join(sheet.name for sheet in parsed.sheets)
        raise QueryError(
            f"This file has several worksheets; name one of: {names}."
        )

    for sheet in parsed.sheets:
        if sheet.name.lower() == sheet_name.lower():
            return sheet

    names = ", ".join(sheet.name for sheet in parsed.sheets)
    raise QueryError(f"No worksheet named {sheet_name!r}. Available: {names}.")


def require_column(sheet: SheetContent, column: str) -> str:
    """Resolve a column name case-insensitively, or raise with the options."""

    for name in sheet.column_names:
        if name.lower() == column.lower():
            return name

    available = ", ".join(sheet.column_names)
    raise QueryError(
        f"No column {column!r} in worksheet {sheet.name!r}. "
        f"Available: {available}."
    )


# ============================================================
# Filtering
# ============================================================


def apply_filters(
    sheet: SheetContent,
    filters: list[Filter],
) -> list[dict[str, Any]]:
    """Every row matching all filters, in file order."""

    # [claude] The tool layer parses dicts into Filters; the service takes
    # Filters. Passing a dict straight through used to fail with
    # "'dict' object has no attribute 'column'" several frames deep, which
    # says nothing about what to do instead.
    for filter in filters:
        if not isinstance(filter, Filter):
            raise QueryError(
                f"Filters must be Filter objects, got {type(filter).__name__}. "
                "The dict form is accepted by the workspace tools, not here."
            )

    resolved = [
        Filter(
            column=require_column(sheet, filter.column),
            op=filter.op,
            value=filter.value,
        )
        for filter in filters
    ]

    for filter in resolved:
        if filter.op not in OPERATORS:
            raise QueryError(
                f"Unknown operator {filter.op!r}. "
                f"Use one of: {', '.join(OPERATORS)}."
            )

    return [
        row
        for row in sheet.rows
        if all(_matches(row.get(f.column), f) for f in resolved)
    ]


def _matches(value: Any, filter: Filter) -> bool:
    op = filter.op

    if op == "is_null":
        return value is None or value == ""

    if op == "is_not_null":
        return value is not None and value != ""

    if value is None:
        # SQL semantics: NULL compares false to everything.
        return False

    if op == "contains":
        return str(filter.value).lower() in str(value).lower()

    left, right = _comparable(value, filter.value)

    if left is None or right is None:
        return False

    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right

    return False


def _comparable(value: Any, other: Any) -> tuple[Any, Any]:
    """
    Coerce both sides to a comparable pair.

    [claude] Numeric where both sides are numeric, string otherwise. This is
    the one place a spreadsheet's untyped-ness has to be papered over: a CSV
    holds "1200" and the agent filters on 1200, and refusing to match those
    would make the tool useless. Comparing as text instead would put "90"
    above "1200", which is worse than either.
    """

    left = _as_number(value)
    right = _as_number(other)

    if left is not None and right is not None:
        return left, right

    return str(value).strip().lower(), str(other).strip().lower()


def _as_number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return Decimal(str(value))

    if isinstance(value, str):
        candidate = value.strip().replace(",", "")

        if not candidate:
            return None

        try:
            return Decimal(candidate)
        except (InvalidOperation, ValueError):
            return None

    return None


# ============================================================
# Aggregation
# ============================================================


def aggregate(
    sheet: SheetContent,
    operation: str,
    column: str | None = None,
    group_by: str | None = None,
    filters: list[Filter] | None = None,
) -> dict[str, Any]:
    """
    Compute one aggregate over every matching row.

    Non-numeric values are counted and reported separately rather than
    treated as zero. A column holding "N/A" among its numbers would
    otherwise deflate an average silently — the same class of error as the
    denominator bugs in docs/HANDOFF.md, arriving from the file side.
    """

    if operation not in AGGREGATIONS:
        raise QueryError(
            f"Unknown aggregation {operation!r}. "
            f"Use one of: {', '.join(AGGREGATIONS)}."
        )

    if operation != "count" and not column:
        raise QueryError(f"{operation!r} needs a column.")

    rows = apply_filters(sheet, filters or [])

    resolved_column = require_column(sheet, column) if column else None
    resolved_group = require_column(sheet, group_by) if group_by else None

    if resolved_group is None:
        value, skipped = _compute(rows, operation, resolved_column)

        return {
            "operation": operation,
            "column": resolved_column,
            "matched_rows": len(rows),
            "value": value,
            "non_numeric_skipped": skipped,
        }

    groups: dict[str, list[dict[str, Any]]] = {}

    for row in rows:
        key = row.get(resolved_group)
        groups.setdefault("(blank)" if key in (None, "") else str(key), []).append(row)

    results = {}

    for key, members in groups.items():
        value, skipped = _compute(members, operation, resolved_column)
        results[key] = {
            "value": value,
            "rows": len(members),
            "non_numeric_skipped": skipped,
        }

    return {
        "operation": operation,
        "column": resolved_column,
        "group_by": resolved_group,
        "matched_rows": len(rows),
        "groups": dict(
            sorted(results.items(), key=lambda item: item[1]["rows"], reverse=True)
        ),
    }


def _compute(
    rows: list[dict[str, Any]],
    operation: str,
    column: str | None,
) -> tuple[Any, int]:
    if operation == "count":
        return len(rows), 0

    raw = [row.get(column) for row in rows]
    present = [value for value in raw if value is not None and value != ""]

    numbers = [_as_number(value) for value in present]
    usable = [number for number in numbers if number is not None]
    skipped = len(present) - len(usable)

    if not usable:
        return None, skipped

    if operation == "sum":
        return float(sum(usable)), skipped
    if operation == "avg":
        return float(sum(usable) / len(usable)), skipped
    if operation == "min":
        return float(min(usable)), skipped
    if operation == "max":
        return float(max(usable)), skipped

    return None, skipped


__all__ = [
    "AGGREGATIONS",
    "Filter",
    "MAX_RETURNED_ROWS",
    "OPERATORS",
    "QueryError",
    "aggregate",
    "apply_filters",
    "get_sheet",
    "require_column",
]
