#!/usr/bin/env python3
"""
[claude] Generate tests/fixtures/deals.sql from the SQL catalogue.

Why this exists
---------------
The hand-written fixture described a different database than the catalogue.
It had 16 columns against the catalogue's 62, and the two shared only six
names (id, status, created_at, deleted_at, owner_id, project_id). Worse, the
fixture carried the exact columns the catalogue tells the agent *not* to
invent — `expected_close_date`, `days_in_stage`, `currency`, `value` — so a
model obeying the rules produced SQL that could not run locally, while a model
breaking them appeared to work.

Since there is no access to the real `mytai` database, the catalogue is the
contract. Generating the fixture from it means local tests exercise the column
names the SQL agent will actually emit.

Scope and honesty about it
--------------------------
This produces a *shape-faithful* fixture, not a copy of production. Column
names come from the catalogue; types come from the heuristic below plus the
override table, which encodes the documented gotchas (`area` is varchar,
`delivery_date` is a float8 year, `transaction_date` is a timestamp, and so
on). Row counts are tiny; only the enum ratios and soft-delete ratio echo the
real distribution.

Masked columns are absent, because the catalogue omits them. The fixture
mirrors the agent's permitted surface and nothing else — no masking logic is
implemented or needed here.

Usage
-----
    python scripts/generate_fixture.py            # writes the fixture
    python scripts/generate_fixture.py --report   # print inferred types, write nothing

Load it with:
    psql "$DATABASE_URL" -f tests/fixtures/deals.sql
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.sql.catalogue import (  # noqa: E402
    DEALS_ENUMS,
    DEALS_TABLE,
    LEADS_TABLE,
    USERS_TABLE,
    Table,
)

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "deals.sql"

# Deterministic output — regenerating must not produce a spurious diff.
SEED = 20260812

N_USERS = 12
N_LEADS = 300
N_DEALS = 120

# Ratios taken from the schema document's live-row counts.
DEAL_STATUS_WEIGHTS = {
    "contracted": 24617,
    "cancelled": 8040,
    "eoi": 3101,
    "reservation": 671,
}
DEAL_SOFT_DELETE_RATE = 1 - (36429 / 40156)   # ~9.3%
LEAD_SOFT_DELETE_RATE = 1 - (4758963 / 5358628)  # ~11.2%

# `lead_stages` has 16 rows with ids 1-14, 16, 17 — id 15 does not exist.
LEAD_STAGE_IDS = [*range(1, 15), 16, 17]


# ============================================================
# Enum types
# ============================================================

ENUM_TYPE_NAMES = {
    "status": "deals_status_enum",
    "selling_type": "deals_selling_type_enum",
    "franchise_owner_approval": "deals_franchise_owner_approval_enum",
    "sales_operation_approval": "deals_sales_operation_approval_enum",
    "collection_approval": "deals_collection_approval_enum",
    "collection_amount_status": "deals_collection_amount_status_enum",
}


# ============================================================
# Type resolution
# ============================================================
#
# The catalogue stores only a name and a description per column, so types are
# inferred. The heuristic is deliberately dumb; every place the schema document
# contradicts it is listed in TYPE_OVERRIDES rather than folded into the rules.
# Run with --report to audit what came out.

TYPE_OVERRIDES: dict[tuple[str, str], str] = {
    # ---- deals: enums -------------------------------------------------
    ("deals", "status"): "deals_status_enum NOT NULL",
    ("deals", "selling_type"): "deals_selling_type_enum NOT NULL",
    ("deals", "franchise_owner_approval"): (
        "deals_franchise_owner_approval_enum NOT NULL"
    ),
    ("deals", "sales_operation_approval"): (
        "deals_sales_operation_approval_enum NOT NULL"
    ),
    ("deals", "collection_approval"): "deals_collection_approval_enum NOT NULL",
    ("deals", "collection_amount_status"): (
        "deals_collection_amount_status_enum NOT NULL"
    ),
    # ---- deals: documented gotchas ------------------------------------
    # float8 holding a YEAR number, not a date (schema doc §7.2).
    ("deals", "delivery_date"): "DOUBLE PRECISION",
    # varchar, so numeric work needs an explicit cast (catalogue rule 22).
    ("deals", "area"): "VARCHAR(255)",
    # These are timestamps despite the _date suffix.
    ("deals", "reservation_date"): "TIMESTAMP",
    ("deals", "contract_date"): "TIMESTAMP",
    ("deals", "cancellation_date"): "TIMESTAMP",
    ("deals", "transaction_date"): "TIMESTAMP",
    # These are dates despite the _at suffix.
    ("deals", "collected_at"): "DATE",
    # Identifier-shaped names that are not identifiers.
    ("deals", "national_id"): "VARCHAR(255)",
    # ---- deals: remaining scalars -------------------------------------
    ("deals", "source_resolution_flow"): "SMALLINT",
    ("deals", "lead_occurrence_count"): "INTEGER NOT NULL DEFAULT 0",
    ("deals", "cumulative_sales_at_deal"): "NUMERIC",
    ("deals", "is_commercial"): "BOOLEAN NOT NULL DEFAULT FALSE",
    ("deals", "has_retroactive_adjustments"): "BOOLEAN NOT NULL DEFAULT FALSE",
    ("deals", "client_name"): "VARCHAR(255) NOT NULL",
    ("deals", "last_comment"): "TEXT",
    # ---- leads: identifier-shaped names that are varchar ---------------
    ("leads", "tai_id"): "VARCHAR(255)",
    ("leads", "facebook_ad_id"): "VARCHAR(255)",
    ("leads", "facebook_ad_account_id"): "VARCHAR(255)",
    ("leads", "tiktok_leadgen_id"): "VARCHAR(255)",
    ("leads", "tiktok_ad_id"): "VARCHAR(255)",
    ("leads", "tiktok_campaign_id"): "VARCHAR(255)",
    ("leads", "tiktok_advertiser_id"): "VARCHAR(255)",
    ("leads", "tiktok_adgroup_id"): "VARCHAR(255)",
    ("leads", "snapchat_leadgen_id"): "VARCHAR(255)",
    ("leads", "snapchat_ad_id"): "VARCHAR(255)",
    ("leads", "snapchat_campaign_id"): "VARCHAR(255)",
    ("leads", "snapchat_ad_set_id"): "VARCHAR(255)",
    ("leads", "google_sheet_sync_uuid"): "UUID",
    # ---- leads: *_by columns are user ids ------------------------------
    ("leads", "qualified_by"): "BIGINT",
    ("leads", "last_comment_by"): "BIGINT",
    # ---- leads: timestamps despite the _date suffix ---------------------
    ("leads", "last_activity_date"): "TIMESTAMP",
    # ---- leads: numerics ------------------------------------------------
    ("leads", "leads_mart_id"): "INTEGER",
    ("leads", "leads_mart_campaign_id"): "INTEGER",
    ("leads", "escalation_level"): "SMALLINT NOT NULL DEFAULT 0",
    ("leads", "potential_review_level"): "SMALLINT NOT NULL DEFAULT 0",
    ("leads", "activity_score_boost"): "INTEGER NOT NULL DEFAULT 0",
    ("leads", "engagement_score"): "INTEGER NOT NULL DEFAULT 0",
    # ---- leads: not-null flags and statuses -----------------------------
    ("leads", "name"): "VARCHAR(255) NOT NULL",
    ("leads", "is_stale"): "BOOLEAN NOT NULL DEFAULT FALSE",
    ("leads", "is_duplicated"): "BOOLEAN NOT NULL DEFAULT FALSE",
    ("leads", "is_bayty"): "BOOLEAN NOT NULL DEFAULT FALSE",
    ("leads", "is_autodialing_enabled"): "BOOLEAN NOT NULL DEFAULT FALSE",
    ("leads", "been_new_lead"): "BOOLEAN NOT NULL DEFAULT FALSE",
    ("leads", "potential_review_kept"): "BOOLEAN NOT NULL DEFAULT FALSE",
    ("leads", "qualification_status"): "VARCHAR(255) NOT NULL DEFAULT 'pending'",
    ("leads", "budget_status"): "VARCHAR(255) NOT NULL DEFAULT 'unknown'",
    ("leads", "authority_status"): "VARCHAR(255) NOT NULL DEFAULT 'unknown'",
    ("leads", "need_status"): "VARCHAR(255) NOT NULL DEFAULT 'unknown'",
    ("leads", "timeline_status"): "VARCHAR(255) NOT NULL DEFAULT 'unknown'",
    ("leads", "consent_status"): "VARCHAR(255) NOT NULL DEFAULT 'unknown'",
    ("leads", "last_comment_text"): "TEXT",
    # ---- users -----------------------------------------------------------
    ("users", "name"): "VARCHAR(255) NOT NULL",
}


def infer_type(table_name: str, column_name: str) -> str:
    """Resolve a PostgreSQL type for a catalogue column."""

    override = TYPE_OVERRIDES.get((table_name, column_name))
    if override is not None:
        return override

    if column_name == "id":
        return "BIGINT PRIMARY KEY"
    if column_name.endswith("_id"):
        return "BIGINT"
    if column_name.endswith("_at"):
        return "TIMESTAMP"
    if column_name.endswith("_date"):
        return "DATE"
    if column_name.startswith(("is_", "has_", "been_")):
        return "BOOLEAN"
    if column_name.endswith(("_count", "_score", "_level", "_minutes")):
        return "INTEGER"

    return "VARCHAR(255)"


# ============================================================
# SQL literal helpers
# ============================================================


@dataclass(frozen=True)
class RawSql:
    """[claude] An expression emitted verbatim rather than quoted."""

    sql: str


@dataclass(frozen=True)
class Rel:
    """
    [claude] A date expressed as an offset from CURRENT_DATE, negative for the
    past.

    Dates were originally emitted as absolute literals anchored to a
    hardcoded 2024-01-01. That put the entire fixture in the past: by the
    time it was loaded, not one deal had a future expected_closing_date, so
    "which deals are closing soon?" answered with deals that had closed two
    years earlier, and any "last 30 days" or "this year" question returned
    nothing at all.

    Emitting an interval expression instead keeps the generated file
    byte-stable — regenerating on a different day produces no diff — while
    the data stays correctly positioned relative to whenever it is loaded.
    """

    days: int
    as_timestamp: bool = False

    def sql(self) -> str:
        sign = "+" if self.days >= 0 else "-"
        base = "CURRENT_TIMESTAMP" if self.as_timestamp else "CURRENT_DATE"
        cast = "" if self.as_timestamp else "::date"
        return f"({base} {sign} INTERVAL '{abs(self.days)} days'){cast}"

    def shift(self, days: int) -> Rel:
        return Rel(self.days + days, self.as_timestamp)

    def as_ts(self) -> Rel:
        return Rel(self.days, True)


def lit(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Rel):  # [claude] relative date expression
        return value.sql()
    if isinstance(value, RawSql):  # [claude] verbatim expression
        return value.sql
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value.strftime('%Y-%m-%d %H:%M:%S')}'"
    if isinstance(value, date):
        return f"'{value.isoformat()}'"
    return "'" + str(value).replace("'", "''") + "'"


def insert_stmt(table: str, columns: list[str], rows: list[dict]) -> str:
    if not rows:
        return ""

    column_list = ", ".join(columns)
    values = ",\n    ".join(
        "(" + ", ".join(lit(row.get(column)) for column in columns) + ")"
        for row in rows
    )
    return f"INSERT INTO {table} ({column_list}) VALUES\n    {values};\n"


# ============================================================
# DDL
# ============================================================


def render_create_table(table: Table) -> str:
    lines = [f"CREATE TABLE {table.name} ("]

    rendered = [
        f"    {column.name} {infer_type(table.name, column.name)}"
        for column in table.columns
    ]
    lines.append(",\n".join(rendered))
    lines.append(");")

    return "\n".join(lines)


def render_enum_types() -> str:
    blocks = []
    for column_name, type_name in ENUM_TYPE_NAMES.items():
        values = ", ".join(lit(v) for v in DEALS_ENUMS[column_name])
        blocks.append(f"CREATE TYPE {type_name} AS ENUM ({values});")
    return "\n".join(blocks)


# ============================================================
# Seed data
# ============================================================

FIRST_NAMES = [
    "Sara", "Ahmed", "Mona", "Khaled", "Nour", "Omar",
    "Yasmin", "Tarek", "Hana", "Karim", "Laila", "Youssef",
]
LAST_NAMES = [
    "Mostafa", "Ibrahim", "Hassan", "Fouad", "Saleh", "Nasser",
    "Adel", "Zaki", "Rashad", "Halim", "Mansour", "Darwish",
]
PROJECTS = [101, 102, 103, 104, 105]
NATIONALITIES = ["Egyptian", "Saudi", "Emirati", "Jordanian"]
CITIES = ["Cairo", "Giza", "Alexandria", "New Cairo", "Sheikh Zayed"]


def build_users(rng: random.Random) -> list[dict]:
    """A small reporting tree. users.parent_id is what every scope walks."""

    rows = []
    for index in range(N_USERS):
        user_id = index + 1
        # First three are roots; everyone else reports into an earlier user.
        parent_id = None if index < 3 else rng.randint(1, max(3, index))
        rows.append(
            {
                "id": user_id,
                # Sara Mostafa is index 0 — the catalogue's worked example
                # ("How many deals does Sara Mostafa own?") must be answerable.
                "name": f"{FIRST_NAMES[index]} {LAST_NAMES[index]}",
                "parent_id": parent_id,
            }
        )
    return rows


def build_leads(rng: random.Random) -> list[dict]:
    rows = []

    for index in range(N_LEADS):
        # [claude] Spread over the two years up to today, rather than from a
        # fixed 2024 anchor that drifts further into the past over time.
        created = Rel(-rng.randint(0, 730), as_timestamp=True)
        deleted = (
            created.shift(rng.randint(1, 200))
            if rng.random() < LEAD_SOFT_DELETE_RATE
            else None
        )
        merged_into = (
            rng.randint(1, N_LEADS) if rng.random() < 0.08 else None
        )

        rows.append(
            {
                "id": index + 1,
                "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                "created_at": created,
                "updated_at": created.shift(rng.randint(0, 60)),
                "deleted_at": deleted,
                "agent_id": rng.randint(1, N_USERS),
                "creator_id": rng.randint(1, N_USERS),
                "team_leader_id": rng.randint(1, N_USERS),
                "franchise_id": rng.randint(1, 5),
                "lead_stage_id": rng.choice(LEAD_STAGE_IDS),
                "current_stage_entered_at": created.shift(rng.randint(0, 30)),
                "is_stale": rng.random() < 0.22,
                "escalation_level": rng.choice([0, 0, 0, 1, 2]),
                "been_new_lead": True,
                "lead_source_id": rng.randint(1, 20),
                "lead_channel_id": rng.randint(1, 10),
                "project_id": rng.choice(PROJECTS),
                "qualification_status": rng.choice(
                    ["pending", "qualified", "unqualified"]
                ),
                "qualification_score": rng.randint(0, 100),
                "budget_status": rng.choice(["unknown", "met", "below"]),
                "authority_status": rng.choice(["unknown", "confirmed"]),
                "need_status": rng.choice(["unknown", "confirmed"]),
                "timeline_status": rng.choice(["unknown", "immediate", "later"]),
                "activity_score_boost": rng.randint(0, 20),
                "engagement_score": rng.randint(0, 100),
                "predictive_score": rng.randint(0, 100),
                "potential_review_level": 0,
                "potential_review_kept": False,
                "last_activity_type": rng.choice(["call", "message", "meeting"]),
                "last_activity_status": rng.choice(["done", "missed"]),
                "last_activity_date": created.shift(rng.randint(0, 90)),
                "is_duplicated": merged_into is not None,
                "merged_into_id": merged_into,
                "is_bayty": False,
                "is_autodialing_enabled": rng.random() < 0.3,
                "consent_status": rng.choice(["granted", "unknown"]),
                "utm_source": rng.choice(["facebook", "tiktok", "google", None]),
                "response_time_minutes": rng.randint(1, 2000),
            }
        )
    return rows


def _weighted_status(rng: random.Random) -> str:
    total = sum(DEAL_STATUS_WEIGHTS.values())
    point = rng.random() * total
    running = 0.0
    for status, weight in DEAL_STATUS_WEIGHTS.items():
        running += weight
        if point <= running:
            return status
    return "contracted"


def build_deals(rng: random.Random) -> list[dict]:
    rows = []

    for index in range(N_DEALS):
        # [claude] Anchored to today, so a share of expected_closing_date
        # values land in the future and "closing soon" means something.
        created = Rel(-rng.randint(0, 730), as_timestamp=True)
        status = _weighted_status(rng)
        deleted = (
            created.shift(rng.randint(1, 200))
            if rng.random() < DEAL_SOFT_DELETE_RATE
            else None
        )
        transaction = created.shift(rng.randint(0, 45))

        rows.append(
            {
                "id": index + 1,
                "status": status,
                "created_at": created,
                "updated_at": created.shift(rng.randint(0, 90)),
                "deleted_at": deleted,
                "created_method": rng.choice(["manual", "import", "api"]),
                "batch_date": Rel(created.days + 30),
                "batch_number": f"B-{2024 + index % 2}-{index % 40:03d}",
                "agent_id": rng.randint(1, N_USERS),
                "creator_id": rng.randint(1, N_USERS),
                "team_leader_id": rng.randint(1, N_USERS),
                "franchise_id": rng.randint(1, 5),
                "owner_id": rng.randint(1, N_USERS),
                "lead_id": rng.randint(1, N_LEADS),
                "deal_source_id": rng.randint(1, 20),
                # Misleadingly named: holds a leads.id (catalogue rule 17).
                "deal_lead_source_id": rng.randint(1, N_LEADS),
                "last_lead_source_id": rng.randint(1, 20),
                "source_resolution_flow": rng.choice([None, 1, 2, 3]),
                "lead_occurrence_count": rng.randint(1, 5),
                "unit_number": f"U-{rng.randint(100, 999)}",
                "project_id": rng.choice(PROJECTS),
                "developer_id": rng.randint(1, 8),
                "location_id": rng.randint(1, 12),
                "unit_type_id": rng.randint(1, 6),
                "finishing_type_id": rng.randint(1, 4),
                # varchar on purpose — numeric use requires a cast.
                "area": str(rng.randint(80, 450)),
                "selling_type": rng.choice(["primary", "resale"]),
                # float8 holding a year number, not a date.
                "delivery_date": RawSql(
                    f"(EXTRACT(YEAR FROM CURRENT_DATE) + {rng.randint(1, 4)})"
                ),
                "payment_plan": rng.choice(["8 years", "5 years", "cash"]),
                "franchise_owner_approval": rng.choice(
                    ["pending", "accepted", "rejected"]
                ),
                "sales_operation_approval": rng.choice(
                    ["pending", "accepted", "rejected"]
                ),
                "collection_approval": rng.choice(
                    ["pending", "accepted", "rejected"]
                ),
                "collection_amount_status": rng.choice(
                    ["pending", "half_collected", "fully_collected"]
                ),
                "collected_at": Rel(created.days + 60),
                "is_commercial": rng.random() < 0.18,
                "reservation_date": created.shift(rng.randint(0, 20)),
                "contract_date": (
                    created.shift(rng.randint(20, 120))
                    if status == "contracted"
                    else None
                ),
                "contract_date_added_at": (
                    created.shift(rng.randint(20, 130))
                    if status == "contracted"
                    else None
                ),
                "cancellation_date": (
                    created.shift(rng.randint(10, 200))
                    if status == "cancelled"
                    else None
                ),
                "transaction_date": transaction,
                # [claude] Offsets reach well past `created`, so recent
                # deals produce genuinely upcoming closing dates.
                "expected_closing_date": Rel(
                    created.days + rng.randint(5, 400)
                ),
                "client_name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                "national_id": str(rng.randint(10**13, 10**14 - 1)),
                "birth_date": date(
                    rng.randint(1965, 2000), rng.randint(1, 12), rng.randint(1, 28)
                ),
                "nationality": rng.choice(NATIONALITIES),
                "job_title": rng.choice(["Engineer", "Doctor", "Manager", "Teacher"]),
                "city": rng.choice(CITIES),
                "country": "Egypt",
                "cumulative_sales_at_deal": rng.randint(0, 50) * 1000,
                "has_retroactive_adjustments": rng.random() < 0.1,
                "last_comment": rng.choice([None, "Follow up next week", "Docs sent"]),
            }
        )
    return rows


# ============================================================
# Render
# ============================================================


def render_fixture() -> str:
    rng = random.Random(SEED)

    users = build_users(rng)
    leads = build_leads(rng)
    deals = build_deals(rng)

    parts = [
        "-- ============================================================",
        "-- MarQ Agent — local test database",
        "--",
        "-- [claude] GENERATED FILE — do not edit by hand.",
        "-- Regenerate with:  python scripts/generate_fixture.py",
        "--",
        "-- Columns are generated from app/sql/catalogue.py so that local",
        "-- tests exercise the same names the SQL agent emits. Types come",
        "-- from the inference rules and override table in that script.",
        "--",
        "-- Masked columns are absent: the catalogue omits them, and this",
        "-- fixture mirrors the agent's permitted surface exactly.",
        "--",
        "-- TEST DATA ONLY — synthetic, deterministic (seed "
        f"{SEED}).",
        "-- ============================================================",
        "",
        "DROP TABLE IF EXISTS deals;",
        "DROP TABLE IF EXISTS leads;",
        "DROP TABLE IF EXISTS users;",
        "",
    ]

    parts += [
        f"DROP TYPE IF EXISTS {name};" for name in ENUM_TYPE_NAMES.values()
    ]
    parts += ["", render_enum_types(), ""]

    for table in (USERS_TABLE, LEADS_TABLE, DEALS_TABLE):
        parts += [render_create_table(table), ""]

    for table, rows in (
        (USERS_TABLE, users),
        (LEADS_TABLE, leads),
        (DEALS_TABLE, deals),
    ):
        # Only emit columns the seed builder actually populates; the rest keep
        # their NULL/default, which is itself realistic for this schema.
        populated = [
            column.name
            for column in table.columns
            if any(column.name in row for row in rows)
        ]
        parts += [
            f"-- {table.name}: {len(rows)} rows, "
            f"{len(populated)}/{len(table.columns)} columns populated",
            insert_stmt(table.name, populated, rows),
        ]

    return "\n".join(parts)


def print_report() -> None:
    for table in (USERS_TABLE, LEADS_TABLE, DEALS_TABLE):
        print(f"\n=== {table.name} ({len(table.columns)} columns) ===")
        for column in table.columns:
            resolved = infer_type(table.name, column.name)
            source = (
                "override"
                if (table.name, column.name) in TYPE_OVERRIDES
                else "inferred"
            )
            print(f"  {column.name:38} {resolved:45} [{source}]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="store_true",
        help="print resolved column types and exit without writing",
    )
    args = parser.parse_args()

    if args.report:
        print_report()
        return

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(render_fixture(), encoding="utf-8")
    print(f"wrote {FIXTURE_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
