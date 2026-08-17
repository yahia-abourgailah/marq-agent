"""
[claude] Reconciliation between uploaded rows and CRM rows.

Key normalisation gets the most attention here because it is where real
reconciliations fail. A comparison that reports every row as missing from
both sides — because one side quoted its ids and the other did not — is
worse than useless: it is a confident, specific, entirely wrong finding.
"""

from __future__ import annotations

import pytest

from app.workspace.compare import CompareError, compare_rows, normalise_key

# ============================================================
# Key normalisation
# ============================================================


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("  ABC-001 ", "abc-001"),
        ("D-1", "d-1"),
        (1024, "1024"),
        (1024.0, "1024"),
        ("1024", 1024),
        (1024.0, 1024),
        ("0001", "1"),
    ],
)
def test_keys_that_mean_the_same_thing_normalise_alike(left, right):
    assert normalise_key(left) == normalise_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [("D-1", "D-2"), ("abc", "abd"), (1, 2)],
)
def test_different_keys_stay_different(left, right):
    assert normalise_key(left) != normalise_key(right)


def test_large_numeric_keys_do_not_become_scientific_notation():
    """Decimal.normalize() renders 1000 as 1E+3 unless it is expanded back."""

    assert normalise_key(1000) == normalise_key("1000")
    assert "E" not in normalise_key(1000000)


# ============================================================
# The four outcomes
# ============================================================


UPLOADED = [
    {"deal_id": "D-1", "amount": 1000},
    {"deal_id": "D-2", "amount": 2500},
    {"deal_id": "D-3", "amount": 400},
]

CRM = [
    {"id": "D-1", "amount": 1000},
    {"id": "D-2", "amount": 9999},
    {"id": "D-9", "amount": 50},
]


def test_all_four_outcomes_are_reported():
    result = compare_rows(
        UPLOADED, CRM, key="deal_id", right_key="id", value_columns=["amount"]
    )

    totals = result["totals"]

    assert totals["matched"] == 1  # D-1
    assert totals["mismatched"] == 1  # D-2
    assert totals["only_in_uploaded"] == 1  # D-3
    assert totals["only_in_crm"] == 1  # D-9


def test_a_mismatch_reports_both_values_and_the_difference():
    result = compare_rows(
        UPLOADED, CRM, key="deal_id", right_key="id", value_columns=["amount"]
    )

    difference = result["mismatched"][0]["differences"][0]

    assert difference["column"] == "amount"
    assert difference["uploaded"] == 2500
    assert difference["crm"] == 9999
    assert difference["difference"] == pytest.approx(2500 - 9999)


def test_rows_present_on_one_side_only_are_listed_by_key():
    result = compare_rows(
        UPLOADED, CRM, key="deal_id", right_key="id", value_columns=["amount"]
    )

    assert result["only_in_uploaded"] == ["d-3"]
    assert result["only_in_crm"] == ["d-9"]


def test_identifiers_differing_only_in_formatting_still_match():
    """The whole reason normalise_key exists, exercised end to end."""

    result = compare_rows(
        [{"deal_id": " D-1 ", "amount": 100}],
        [{"deal_id": "d-1", "amount": 100}],
        key="deal_id",
    )

    assert result["totals"]["matched"] == 1
    assert result["totals"]["only_in_uploaded"] == 0


def test_numeric_values_agree_within_tolerance():
    """Spreadsheet money is rounded; the CRM keeps more places."""

    result = compare_rows(
        [{"id": 1, "amount": 1000.00}],
        [{"id": 1, "amount": 1000.004}],
        key="id",
    )

    assert result["totals"]["matched"] == 1


def test_numeric_values_outside_tolerance_are_a_mismatch():
    result = compare_rows(
        [{"id": 1, "amount": 1000.00}],
        [{"id": 1, "amount": 1000.5}],
        key="id",
    )

    assert result["totals"]["mismatched"] == 1


def test_text_values_compare_case_insensitively_after_trimming():
    result = compare_rows(
        [{"id": 1, "status": " Contracted "}],
        [{"id": 1, "status": "contracted"}],
        key="id",
    )

    assert result["totals"]["matched"] == 1


def test_numbers_stored_as_text_match_real_numbers():
    result = compare_rows(
        [{"id": 1, "amount": "1000"}],
        [{"id": 1, "amount": 1000}],
        key="id",
    )

    assert result["totals"]["matched"] == 1


# ============================================================
# Edge cases worth reporting rather than swallowing
# ============================================================


def test_duplicate_keys_are_warned_about_not_silently_collapsed():
    """
    [claude] A spreadsheet listing the same deal twice is itself a finding.
    Collapsing it silently answers a subtly different question than the one
    asked.
    """

    result = compare_rows(
        [{"id": 1, "amount": 10}, {"id": 1, "amount": 20}],
        [{"id": 1, "amount": 10}],
        key="id",
    )

    assert result["totals"]["matched"] == 1
    assert any("duplicate key" in warning for warning in result["warnings"])


def test_comparing_with_no_shared_columns_is_not_reported_as_matched():
    """
    [claude] The most dangerous output this module can produce. With nothing
    to compare, every common key falls through to "no difference found" — and
    reporting that as `matched: 38` is indistinguishable from 38 rows
    genuinely agreeing. A real agent run relayed exactly that to the user.

    So `matched` must be absent entirely, and the count renamed to something
    that cannot be read as agreement.
    """

    result = compare_rows(
        [{"id": 1, "a": 1}],
        [{"id": 1, "b": 2}],
        key="id",
    )

    totals = result["totals"]

    assert "matched" not in totals
    assert "mismatched" not in totals
    assert totals["present_on_both"] == 1
    assert totals["values_compared"] is False

    assert any("NO VALUES WERE COMPARED" in w for w in result["warnings"])


def test_value_columns_can_be_named_differently_on_each_side():
    """
    [claude] From a real run: the sheet column was `Area (sqm)` and the CRM
    column was `area`. key/crm_key could express that for the identifier and
    nothing could express it for the values, so the comparison degraded to
    checking presence only.
    """

    result = compare_rows(
        [{"Deal ID": 4, "Area (sqm)": 207.5}, {"Deal ID": 6, "Area (sqm)": 468}],
        [{"id": 4, "area": "195"}, {"id": 6, "area": "468"}],
        key="Deal ID",
        right_key="id",
        value_columns=["Area (sqm)"],
        right_value_columns=["area"],
    )

    assert result["totals"]["matched"] == 1
    assert result["totals"]["mismatched"] == 1
    assert result["mismatched"][0]["key"] == "4"


def test_mismatched_value_column_lists_are_rejected():
    with pytest.raises(CompareError, match="same length"):
        compare_rows(
            [{"id": 1, "a": 1}],
            [{"id": 1, "b": 1}],
            key="id",
            value_columns=["a", "b"],
            right_value_columns=["b"],
        )


def test_value_columns_restrict_what_is_compared():
    result = compare_rows(
        [{"id": 1, "amount": 100, "note": "mine"}],
        [{"id": 1, "amount": 100, "note": "theirs"}],
        key="id",
        value_columns=["amount"],
    )

    assert result["totals"]["matched"] == 1


def test_omitting_value_columns_compares_every_shared_column():
    result = compare_rows(
        [{"id": 1, "amount": 100, "note": "mine"}],
        [{"id": 1, "amount": 100, "note": "theirs"}],
        key="id",
    )

    assert result["totals"]["mismatched"] == 1


def test_a_missing_key_column_raises_with_the_available_names():
    with pytest.raises(CompareError, match="amount"):
        compare_rows([{"amount": 1}], [{"id": 1}], key="deal_id")


# ============================================================
# Column resolution — found by a real agent run
# ============================================================


def test_serving_artefacts_in_keys_do_not_break_the_match():
    """
    [claude] Regression from a live trace. vLLM emitted row dictionaries whose
    keys carried control-token artefacts — `<|"|>Deal ID<|"|>` instead of
    `Deal ID` — so the agent's entirely correct key="Deal ID" did not match.
    The old error then printed the mangled keys raw, so it read as "no column
    'Deal ID' ... available: Deal ID", and the agent retried the identical
    call until it exhausted its step budget.
    """

    uploaded = [
        {'<|"|>Deal ID<|"|>': 1, '<|"|>Area<|"|>': 207.5},
        {'<|"|>Deal ID<|"|>': 2, '<|"|>Area<|"|>': 88},
    ]
    crm = [{"id": 1, "Area": 195}, {"id": 2, "Area": 88}]

    result = compare_rows(
        uploaded, crm, key="Deal ID", right_key="id", value_columns=["Area"]
    )

    assert result["totals"]["matched"] == 1
    assert result["totals"]["mismatched"] == 1
    assert result["key"] == "Deal ID"
    assert result["compared_columns"] == ["Area"]


def test_the_error_message_shows_cleaned_names():
    """An unreadable message is why the agent could not recover."""

    with pytest.raises(CompareError) as exc:
        compare_rows(
            [{'<|"|>Area<|"|>': 1}], [{"id": 1}], key="Deal ID"
        )

    message = str(exc.value)

    assert "<|" not in message
    assert "Area" in message


def test_column_names_resolve_across_spacing_and_case():
    """`Deal ID` on one side and `deal_id` on the other is the normal case."""

    result = compare_rows(
        [{"Deal ID": 1, "Area (sqm)": 100}],
        [{"deal_id": 1, "area (SQM)": 100}],
        key="Deal ID",
        right_key="deal_id",
        value_columns=["Area (sqm)"],
    )

    assert result["totals"]["matched"] == 1


@pytest.mark.parametrize(
    "requested",
    ["Area (sqm)", "Area_sqm", "area sqm", "AREASQM", "area-sqm"],
)
def test_punctuation_differences_in_column_names_still_resolve(requested):
    """
    [claude] From a live trace: the model read `Area (sqm)` from one tool and
    passed `Area_sqm` to the next. Folding case and spacing was not enough —
    the parentheses still made them unequal — so the comparison failed on a
    name that plainly meant the same column.
    """

    result = compare_rows(
        [{"id": 1, "Area (sqm)": 207.5}],
        [{"id": 1, "area": 195}],
        key="id",
        value_columns=[requested],
        right_value_columns=["area"],
    )

    assert result["totals"]["mismatched"] == 1


def test_a_value_column_missing_on_one_side_warns_instead_of_silently_matching():
    """
    [claude] The dangerous one. _row_differences skips any column absent from
    either row, so an unresolvable value column used to mean *nothing was
    compared* and every row came back matched — a reconciliation that finds
    no problems because it looked for none. That reads as good news.
    """

    result = compare_rows(
        [{"id": 1, "amount": 100, "area": 50}],
        [{"id": 1, "amount": 999}],
        key="id",
        value_columns=["amount", "area"],
    )

    # amount still compared, and it genuinely differs.
    assert result["totals"]["mismatched"] == 1
    assert result["compared_columns"] == ["amount"]

    assert any("NOT compared" in warning for warning in result["warnings"])
    assert any("area" in warning for warning in result["warnings"])


def test_no_resolvable_value_column_raises_rather_than_reporting_all_matched():
    with pytest.raises(CompareError, match="both sides"):
        compare_rows(
            [{"id": 1, "area": 50}],
            [{"id": 1, "amount": 999}],
            key="id",
            value_columns=["area"],
        )


def test_empty_sides_are_handled():
    result = compare_rows([], [{"id": 1}], key="id")

    assert result["totals"]["only_in_crm"] == 1

    # With one side empty there are no shared columns, so nothing was
    # compared — and `matched` is withheld rather than reported as 0.
    assert result["totals"]["present_on_both"] == 0
    assert result["totals"]["values_compared"] is False


def test_counts_stay_exact_when_examples_are_capped():
    """The examples are a sample; the totals never are."""

    uploaded = [{"id": i, "amount": i} for i in range(1, 101)]

    result = compare_rows(uploaded, [], key="id")

    assert result["totals"]["only_in_uploaded"] == 100
    assert len(result["only_in_uploaded"]) == result["examples_capped_at"]
