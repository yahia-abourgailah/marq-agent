"""
[claude] Drift guard between the catalogue and the local test fixture.

The previous hand-written fixture had drifted so far from the catalogue that
the two shared only six column names, and it carried the exact identifiers the
catalogue tells the agent not to invent (`expected_close_date`, `days_in_stage`,
`currency`, `value`). Tests passed anyway, because nothing compared them.

These tests read the fixture as text — no database required — so the drift
fails in CI rather than at query time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.sql.catalogue import DEALS_TABLE, LEADS_TABLE, USERS_TABLE

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "deals.sql"

CATALOGUE_TABLES = {
    table.name: {column.name for column in table.columns}
    for table in (DEALS_TABLE, LEADS_TABLE, USERS_TABLE)
}

# Identifiers the catalogue explicitly tells the SQL agent are not real columns.
# They were all present in the old fixture, which is how the mismatch survived.
FORBIDDEN_IDENTIFIERS = [
    "expected_close_date",  # rule 20 — the real column is expected_closing_date
    "days_in_stage",        # rule 42 — must be derived from timestamps
    "currency",             # rule 37 — deals exposes no currency column
]


def _fixture_columns(table_name: str) -> set[str]:
    """Pull the column names out of one CREATE TABLE block."""

    body = re.search(
        rf"CREATE TABLE {table_name} \((.*?)\n\);",
        FIXTURE_PATH.read_text(encoding="utf-8"),
        re.DOTALL,
    )

    assert body, f"no CREATE TABLE block for {table_name} in the fixture"

    return {
        line.strip().split()[0]
        for line in body.group(1).strip().splitlines()
        if line.strip()
    }


@pytest.mark.parametrize("table_name", sorted(CATALOGUE_TABLES))
def test_fixture_columns_match_the_catalogue(table_name):
    expected = CATALOGUE_TABLES[table_name]
    actual = _fixture_columns(table_name)

    missing = expected - actual
    extra = actual - expected

    assert not missing, (
        f"{table_name}: in the catalogue but not the fixture: {sorted(missing)}. "
        "Regenerate with `python scripts/generate_fixture.py`."
    )
    assert not extra, (
        f"{table_name}: in the fixture but not the catalogue: {sorted(extra)}. "
        "Regenerate with `python scripts/generate_fixture.py`."
    )


@pytest.mark.parametrize("identifier", FORBIDDEN_IDENTIFIERS)
def test_fixture_does_not_reintroduce_forbidden_identifiers(identifier):
    """
    A fixture carrying these makes a rule-breaking model look correct locally
    and a rule-following one look broken.
    """

    assert identifier not in FIXTURE_PATH.read_text(encoding="utf-8")


def test_fixture_omits_masked_columns():
    """
    Masked columns are excluded from the catalogue, so they must not appear in
    the fixture either — it mirrors the agent's permitted surface.
    """

    masked = [
        "unit_price",
        "reservation_price",
        "contract_price",
        "collection_price",
        "down_payment",
        "total_retroactive_commission",
        "date_ten_percentage",
        "budget_amount",
        "cost_per_lead",
        "ad_spend_amount",
        "last_activity_feedback",
    ]

    content = FIXTURE_PATH.read_text(encoding="utf-8")
    present = [column for column in masked if column in content]

    assert not present, f"masked columns leaked into the fixture: {present}"
