"""
[claude] Reconciliation between two sets of rows.

This is the point of the whole workspace: the user has a spreadsheet, the CRM
has the truth, and the question is where they disagree.

Pure arithmetic, like app/tools/analysis.py
-------------------------------------------
This module touches neither the database nor the vector index. It receives
two row sets the caller has already retrieved — one from `sql_query`, one
from the parsed spreadsheet — and reports how they line up. Keeping it pure
is what makes it testable against hand-computed expectations, which is the
only way a comparison tool earns any trust.

Four outcomes, always all four
------------------------------
    matched        same key, values agree
    mismatched     same key, values differ
    only_in_left   present in the first set, absent from the second
    only_in_right  the reverse

Reporting all four every time is deliberate. "Do these agree?" answered with
a mismatch count alone hides the rows that are missing entirely, and missing
rows are usually the more serious finding — a deal in the sheet that the CRM
has never heard of is a different problem from one whose amount is stale.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# Cap on the examples returned per outcome. The counts are always exact; only
# the listed rows are capped, so a 10,000-row reconciliation still reports
# truthful totals without returning 10,000 rows into a model's context.
MAX_EXAMPLES = 25

# Relative tolerance for numeric equality. Spreadsheet money is frequently
# rounded to two places while the database keeps more, and flagging every
# such row as a mismatch buries the real ones.
DEFAULT_TOLERANCE = 0.01


class CompareError(ValueError):
    """Raised when the two row sets cannot be lined up at all."""


def normalise_key(value: Any) -> str:
    """
    Canonical form of a join key.

    [claude] Real reconciliations fail on formatting far more often than on
    data: the sheet holds `" ABC-001 "`, the CRM holds `"abc-001"`, and a
    naive equality check reports every row as missing from both sides. Ids
    that arrive as numbers on one side and text on the other — 1024 versus
    "1024" — are the same story, and `float` ids from a spreadsheet arrive
    as 1024.0, so a trailing `.0` is dropped too.
    """

    if value is None:
        return ""

    if isinstance(value, bool):
        return str(value).lower()

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    text = str(value).strip()

    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text.casefold()

    normalised = number.normalize()

    # Decimal.normalize() renders 1E+3 for 1000; expand it back.
    return format(normalised, "f")


def compare_rows(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    key: str,
    value_columns: list[str] | None = None,
    right_key: str | None = None,
    right_value_columns: list[str] | None = None,
    left_label: str = "uploaded",
    right_label: str = "crm",
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """
    Line up two row sets on a key and report where they differ.

    `right_key` covers the normal case where the two sides name the same
    thing differently — `deal_id` in a spreadsheet, `id` in the CRM.
    `value_columns` are the fields compared once keys match; when omitted,
    every column the two sides share is compared.

    [claude] `right_value_columns` extends the same idea to the values, and
    exists because a real run needed it: the spreadsheet column was
    `Area (sqm)` and the CRM column was `area`. There was a way to say the
    identifiers were named differently and no way to say the *values* were,
    so the comparison could only fall back to comparing nothing. Paired by
    position with `value_columns`, exactly as `key`/`right_key` pair.
    """

    if not key:
        raise CompareError("A key column is required to compare rows.")

    right_key = right_key or key

    # Resolve the requested names against the keys actually present, on each
    # side independently. See _resolve_column for why this is not just an
    # exact lookup.
    resolved_key = _resolve_column(left, key, left_label)
    resolved_right_key = _resolve_column(right, right_key, right_label)

    left_index, left_duplicates = _index_by_key(left, resolved_key)
    right_index, right_duplicates = _index_by_key(right, resolved_right_key)

    if value_columns:
        pairs, unresolved = _resolve_value_columns(
            left,
            right,
            value_columns,
            right_value_columns,
            left_label,
            right_label,
        )
    else:
        pairs = [
            (column, column)
            for column in _shared_columns(
                left, right, resolved_key, resolved_right_key
            )
        ]
        unresolved = []

    columns = [_clean(left_column) for left_column, _ in pairs]

    matched: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []

    for key_value, left_row in left_index.items():
        right_row = right_index.get(key_value)

        if right_row is None:
            continue

        differences = _row_differences(
            left_row, right_row, pairs, tolerance, left_label, right_label
        )

        if differences:
            mismatched.append({"key": key_value, "differences": differences})
        else:
            matched.append({"key": key_value})

    only_left = [k for k in left_index if k not in right_index]
    only_right = [k for k in right_index if k not in left_index]

    # [claude] When no value columns could be compared, every key present on
    # both sides lands in `matched` — because no difference was found, since
    # nothing was examined. Reporting that as "matched: 38" is the most
    # dangerous output this module can produce: it is indistinguishable from
    # 38 rows genuinely agreeing, and a real agent run relayed exactly that.
    #
    # So the key is renamed when it would be a lie. A caller cannot read
    # "present_on_both" as "the values agree", and the absence of `matched`
    # is what stops it being reported as agreement.
    if columns:
        agreement = {"matched": len(matched), "mismatched": len(mismatched)}
    else:
        agreement = {"present_on_both": len(matched), "values_compared": False}

    return {
        "success": True,
        "key": _clean(resolved_key),
        "compared_columns": columns,
        "totals": {
            f"{left_label}_rows": len(left),
            f"{right_label}_rows": len(right),
            **agreement,
            f"only_in_{left_label}": len(only_left),
            f"only_in_{right_label}": len(only_right),
        },
        "mismatched": mismatched[:MAX_EXAMPLES],
        f"only_in_{left_label}": only_left[:MAX_EXAMPLES],
        f"only_in_{right_label}": only_right[:MAX_EXAMPLES],
        "examples_capped_at": MAX_EXAMPLES,
        "warnings": _warnings(
            left_duplicates,
            right_duplicates,
            columns,
            left_label,
            right_label,
            unresolved,
        ),
    }


# [claude] Control-token artefacts that leak out of the serving stack into
# generated JSON. A real trace against vLLM produced row dictionaries whose
# keys were `<|"|>Deal ID<|"|>` rather than `Deal ID`, so a perfectly correct
# `key="Deal ID"` did not match, and the error message printed the mangled
# keys raw — telling the agent that the column it had just asked for was both
# missing and available. It retried the identical call until it ran out of
# steps.
ARTEFACT = re.compile(r"<\|[^|]*\|>")


def _clean(name: Any) -> str:
    """The name as a person would write it, with serving artefacts removed."""

    return ARTEFACT.sub("", str(name)).strip()


def _canonical(name: Any) -> str:
    """
    Comparison form: letters and digits only, folded.

    [claude] Everything else is dropped because models rewrite column names
    on the way through. A live trace read `Area (sqm)` from one tool and
    passed `Area_sqm` to the next — spaces became underscores and the
    parentheses vanished. Folding only case and spacing was not enough; the
    brackets still made them unequal, and the comparison failed on names that
    plainly denote the same field.

    Aggressive, and safe in context: this only matches a name the caller
    asked for against names already present in the data, and a collision
    would need two columns identical but for punctuation.
    """

    return "".join(
        character
        for character in _clean(name).casefold()
        if character.isalnum()
    )


def _resolve_column(
    rows: list[dict[str, Any]],
    requested: str,
    label: str,
) -> str:
    """
    Map a requested column name onto the key actually present in `rows`.

    Exact match first, then a canonical one. The forgiveness is deliberate
    and matches `query.require_column` on the spreadsheet side: the agent is
    copying names between two tool results and a serving stack in between,
    and failing on `Deal ID` versus `deal_id` would be pedantry rather than
    safety — the values behind them are the same values.

    Raises with the *cleaned* names, so the message is readable and the agent
    can act on it.
    """

    if not rows:
        return requested

    keys = list(rows[0].keys())

    if requested in keys:
        return requested

    target = _canonical(requested)

    for candidate in keys:
        if _canonical(candidate) == target:
            return candidate

    available = ", ".join(sorted(_clean(candidate) for candidate in keys))

    raise CompareError(
        f"No column {_clean(requested)!r} in the {label} rows. "
        f"Available: {available}."
    )


def _resolve_value_columns(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    requested: list[str],
    right_requested: list[str] | None,
    left_label: str,
    right_label: str,
) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Pair each requested value column with its key on each side.

    [claude] This closes a silent-wrong-answer path. `_row_differences` skips
    any column missing from either row, so before this, a value column whose
    name did not match exactly on both sides was quietly not compared — and
    every row came back **matched**. A reconciliation that finds nothing
    because it compared nothing is the worst possible output of this tool:
    it looks like good news.

    Columns that cannot be resolved are returned as warnings rather than
    raising, so a comparison naming four columns of which one is wrong still
    does useful work — and says what it skipped.
    """

    if right_requested and len(right_requested) != len(requested):
        raise CompareError(
            f"value_columns has {len(requested)} entries and "
            f"{right_label}_value_columns has {len(right_requested)}. "
            "They pair by position, so they must be the same length."
        )

    pairs: list[tuple[str, str]] = []
    unresolved: list[str] = []

    for index, column in enumerate(requested):
        counterpart = right_requested[index] if right_requested else column

        try:
            left_column = _resolve_column(left, column, left_label)
            right_column = _resolve_column(right, counterpart, right_label)
        except CompareError:
            unresolved.append(_clean(column))
            continue

        pairs.append((left_column, right_column))

    if not pairs and requested:
        available_left = ", ".join(
            sorted(_clean(k) for k in (left[0].keys() if left else []))
        )
        available_right = ", ".join(
            sorted(_clean(k) for k in (right[0].keys() if right else []))
        )

        raise CompareError(
            f"None of the columns {unresolved} are present on both sides. "
            f"{left_label} has: {available_left}. "
            f"{right_label} has: {available_right}. "
            f"If the two sides name the same field differently, pass "
            f"{right_label}_value_columns with the matching names in the "
            f"same order."
        )

    return pairs, unresolved


def _index_by_key(
    rows: list[dict[str, Any]],
    key: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """
    Index rows by normalised key, recording duplicates.

    [claude] First occurrence wins, and the duplicates are reported rather
    than silently collapsed. A duplicated key means the comparison is
    answering a slightly different question than the user asked, and they
    should be told — a spreadsheet with the same deal listed twice is itself
    a finding.
    """

    index: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []

    for row in rows:
        key_value = normalise_key(row.get(key))

        if not key_value:
            continue

        if key_value in index:
            duplicates.append(key_value)
            continue

        index[key_value] = row

    return index, duplicates


def _shared_columns(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    key: str,
    right_key: str,
) -> list[str]:
    if not left or not right:
        return []

    shared = set(left[0]) & set(right[0])
    shared -= {key, right_key}

    return sorted(shared)


def _row_differences(
    left_row: dict[str, Any],
    right_row: dict[str, Any],
    pairs: list[tuple[str, str]],
    tolerance: float,
    left_label: str,
    right_label: str,
) -> list[dict[str, Any]]:
    differences = []

    for left_column, right_column in pairs:
        if left_column not in left_row or right_column not in right_row:
            continue

        left_value = left_row[left_column]
        right_value = right_row[right_column]

        if _values_agree(left_value, right_value, tolerance):
            continue

        difference: dict[str, Any] = {
            "column": _clean(left_column),
            left_label: left_value,
            right_label: right_value,
        }

        left_number = _as_number(left_value)
        right_number = _as_number(right_value)

        if left_number is not None and right_number is not None:
            difference["difference"] = float(left_number - right_number)

        differences.append(difference)

    return differences


def _values_agree(left: Any, right: Any, tolerance: float) -> bool:
    if left is None and right is None:
        return True

    if left is None or right is None:
        return False

    left_number = _as_number(left)
    right_number = _as_number(right)

    if left_number is not None and right_number is not None:
        return abs(float(left_number) - float(right_number)) <= tolerance

    return str(left).strip().casefold() == str(right).strip().casefold()


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


def _warnings(
    left_duplicates: list[str],
    right_duplicates: list[str],
    columns: list[str],
    left_label: str,
    right_label: str,
    unresolved: list[str] | None = None,
) -> list[str]:
    warnings = []

    if not columns:
        warnings.append(
            "NO VALUES WERE COMPARED — the two row sets share no columns "
            "beyond the key, so this only checked which keys appear on both "
            "sides. Do not report these rows as agreeing. Name the fields "
            "with value_columns, and their counterparts with "
            f"{right_label}_value_columns if the two sides name them "
            "differently."
        )

    if unresolved:
        listed = ", ".join(unresolved)
        warnings.append(
            f"These columns were not found on both sides and were NOT "
            f"compared: {listed}. Rows counted as matched may still differ "
            f"in them."
        )

    for label, duplicates in (
        (left_label, left_duplicates),
        (right_label, right_duplicates),
    ):
        if duplicates:
            listed = ", ".join(duplicates[:5])
            warnings.append(
                f"{len(duplicates)} duplicate key(s) in the {label} rows "
                f"({listed}); the first occurrence of each was used."
            )

    return warnings


__all__ = [
    "CompareError",
    "DEFAULT_TOLERANCE",
    "MAX_EXAMPLES",
    "compare_rows",
    "normalise_key",
]
