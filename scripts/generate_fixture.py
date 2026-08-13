#!/usr/bin/env python3
"""
[claude] Generate tests/fixtures/deals.sql from the SQL catalogue.

Why this exists
---------------
The original hand-written fixture described a different database than the
catalogue: 16 columns against 62, sharing only six names, and carrying the
exact identifiers the catalogue tells the agent not to invent. A
rule-following model looked broken locally; a rule-breaking one looked
correct.

Since there is no access to the real `mytai` database, the catalogue is the
contract. Column names come from it, column types come from
`scripts/schema_types.py` (transcribed from the schema document), and
`assert_complete()` fails if the two ever disagree.

What is faithful, and what is not
---------------------------------
Faithful: every column name and type, the enum values, NOT NULL constraints,
the documented quirks (`area` varchar, `delivery_date` a float8 year,
`deal_lead_source_id` holding a `leads.id`), the status mix, the soft-delete
rates, and the relationship cardinalities — a lead can own several deals,
a handful of deals have no lead at all.

Not faithful: absolute scale. Production is 5.36M leads against 40K deals, a
133:1 ratio that would need 100k+ leads to produce a few hundred deals. The
fixture uses a workable ratio instead and keeps the *distributions* real.
Where a documented rarity would round to zero rows at this size — deals with
a NULL lead_id are 0.08% of production — a fixed minimum is planted so the
edge case is actually testable. Those are called out at their definitions.

Masked columns are absent, because the catalogue omits them.

Usage
-----
    python scripts/generate_fixture.py             # write the fixture
    python scripts/generate_fixture.py --report    # resolved types, no write
    python scripts/generate_fixture.py --stats     # row/distribution summary
    python scripts/generate_fixture.py --scale 2   # twice the rows

Load it with:
    psql "$DATABASE_URL" -f tests/fixtures/deals.sql
"""

from __future__ import annotations

import argparse
import random
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from schema_types import assert_complete, column_type  # noqa: E402

from app.sql.catalogue import (  # noqa: E402
    DEALS_ENUMS,
    DEALS_TABLE,
    LEADS_TABLE,
    USERS_TABLE,
    Table,
)

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "deals.sql"

# Deterministic output — regenerating must not produce a spurious diff.
SEED = 20260813

# Base row counts, multiplied by --scale.
N_USERS = 40
N_LEADS = 1000
N_DEALS = 350

# ---- distributions measured from the schema document --------------------

# Live deal status mix.
DEAL_STATUS_WEIGHTS = {
    "contracted": 24617,
    "cancelled": 8040,
    "eoi": 3101,
    "reservation": 671,
}

DEAL_SOFT_DELETE_RATE = 1 - (36429 / 40156)      # ~9.3%
LEAD_SOFT_DELETE_RATE = 1 - (4758963 / 5358628)  # ~11.2%

# 708 opportunities against 40,156 deals.
DEAL_HAS_OPPORTUNITY_RATE = 708 / 40156

# source_resolution_flow is NULL for 35,535 of 40,156 rows.
DEAL_HAS_RESOLUTION_FLOW_RATE = 1 - (35535 / 40156)

# deal_lead_source_id: 36,538 non-null, of which 20,659 equal lead_id.
DEAL_HAS_LEAD_SOURCE_RATE = 36538 / 40156
DEAL_LEAD_SOURCE_EQUALS_LEAD_RATE = 20659 / 36538

# `lead_stages` has 16 rows with ids 1-14, 16, 17 — id 15 does not exist.
LEAD_STAGE_IDS = [*range(1, 15), 16, 17]

# Deals with no lead at all are 33 of 40,156 in production — which rounds to
# zero here. Plant a fixed few so the LEFT JOIN case is testable.
DEALS_WITHOUT_LEAD = 4


# ============================================================
# SQL value wrappers
# ============================================================


@dataclass(frozen=True)
class RawSql:
    """An expression emitted verbatim rather than quoted."""

    sql: str


@dataclass(frozen=True)
class Rel:
    """
    A date expressed as an offset from CURRENT_DATE, negative for the past.

    Dates were originally emitted as absolute literals anchored to a
    hardcoded 2024-01-01. That put the entire fixture in the past: by the
    time it was loaded, not one deal had a future expected_closing_date, so
    "which deals are closing soon?" answered with deals that had closed two
    years earlier, and any "last 30 days" question returned nothing.

    An interval expression keeps the generated file byte-stable — no diff on
    regeneration — while the data stays correctly positioned relative to
    whenever it is loaded.
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

    def as_date(self) -> Rel:
        return Rel(self.days, False)


def lit(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Rel):
        return value.sql()
    if isinstance(value, RawSql):
        return value.sql
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def insert_stmts(table: str, columns: list[str], rows: list[dict]) -> str:
    """Emit INSERTs in chunks so no single statement grows unwieldy."""

    if not rows:
        return ""

    chunks = []
    column_list = ", ".join(columns)

    for start in range(0, len(rows), 100):
        block = rows[start : start + 100]
        values = ",\n    ".join(
            "(" + ", ".join(lit(row.get(column)) for column in columns) + ")"
            for row in block
        )
        chunks.append(f"INSERT INTO {table} ({column_list}) VALUES\n    {values};")

    return "\n\n".join(chunks) + "\n"


# ============================================================
# DDL
# ============================================================

ENUM_TYPE_NAMES = {
    "status": "deals_status_enum",
    "selling_type": "deals_selling_type_enum",
    "franchise_owner_approval": "deals_franchise_owner_approval_enum",
    "sales_operation_approval": "deals_sales_operation_approval_enum",
    "collection_approval": "deals_collection_approval_enum",
    "collection_amount_status": "deals_collection_amount_status_enum",
}


def render_create_table(table: Table) -> str:
    lines = [f"CREATE TABLE {table.name} ("]
    lines.append(
        ",\n".join(
            f"    {column.name} {column_type(table.name, column.name)}"
            for column in table.columns
        )
    )
    lines.append(");")
    return "\n".join(lines)


def render_enum_types() -> str:
    return "\n".join(
        f"CREATE TYPE {type_name} AS ENUM "
        f"({', '.join(lit(v) for v in DEALS_ENUMS[column])});"
        for column, type_name in ENUM_TYPE_NAMES.items()
    )


def render_indexes() -> str:
    """
    The indexes the schema document says support the real access paths.

    Not needed at this row count, but they keep EXPLAIN output shaped like
    production and document which axes are meant to be cheap.
    """

    return "\n".join(
        [
            "CREATE INDEX idx_leads_deleted_agent_created "
            "ON leads (deleted_at, agent_id, created_at);",
            "CREATE INDEX idx_leads_deleted_creator ON leads (deleted_at, creator_id);",
            "CREATE INDEX idx_leads_deleted_franchise "
            "ON leads (deleted_at, franchise_id);",
            "CREATE INDEX idx_leads_stage ON leads (lead_stage_id);",
            "CREATE INDEX idx_leads_merged_into ON leads (merged_into_id);",
            "CREATE INDEX idx_deals_agent_transaction "
            "ON deals (agent_id, transaction_date);",
            "CREATE INDEX idx_deals_franchise_transaction_status "
            "ON deals (franchise_id, transaction_date, status);",
            "CREATE INDEX idx_deals_lead ON deals (lead_id);",
            "CREATE INDEX idx_deals_deleted_status ON deals (deleted_at, status);",
        ]
    )


# ============================================================
# Reference data
# ============================================================

FIRST_NAMES = [
    "Sara", "Ahmed", "Mona", "Khaled", "Nour", "Omar", "Yasmin", "Tarek",
    "Hana", "Karim", "Laila", "Youssef", "Dina", "Hassan", "Rana", "Amr",
    "Salma", "Mahmoud", "Farida", "Ziad", "Aya", "Sherif", "Nada", "Bassem",
]
LAST_NAMES = [
    "Mostafa", "Ibrahim", "Hassan", "Fouad", "Saleh", "Nasser", "Adel",
    "Zaki", "Rashad", "Halim", "Mansour", "Darwish", "Kamel", "Shaker",
    "Sabry", "Lotfy", "Wahba", "Gaber", "Rifaat", "Hegazy",
]
ARABIC_NAMES = [
    "سارة مصطفى", "أحمد إبراهيم", "منى حسن", "خالد فؤاد", "نور صالح",
    "عمر ناصر", "ياسمين عادل", "طارق زكي",
]
NATIONALITIES = ["Egyptian", "Saudi", "Emirati", "Jordanian", "Kuwaiti", "Lebanese"]
CITIES = ["Cairo", "Giza", "Alexandria", "New Cairo", "Sheikh Zayed", "6th of October"]
JOB_TITLES = ["Engineer", "Doctor", "Manager", "Teacher", "Accountant", "Consultant"]
SOCIAL_STATUS = ["single", "married", "divorced"]
PAYMENT_PLANS = ["8 years", "5 years", "10 years", "cash", "3 years"]
CREATED_METHODS = ["manual", "import", "api", "google_sheet"]
LOSS_REASONS = ["budget", "timing", "competitor", "unreachable", None, None]
ACTIVITY_TYPES = ["call", "message", "meeting"]
QUALIFICATION = ["pending", "qualified", "unqualified"]

# One lead comes from one channel; populating every attribution family on
# every row would be unrealistic and would inflate the file for nothing.
CHANNELS = ["facebook", "tiktok", "snapchat", "organic", "cold_call", "referral"]
CHANNEL_WEIGHTS = [34, 18, 9, 20, 12, 7]


# ============================================================
# Row builders
# ============================================================


def build_users(rng: random.Random, count: int) -> list[dict]:
    """A three-level reporting tree — users.parent_id is what scopes walk."""

    rows = []
    roots = max(2, count // 10)

    for index in range(count):
        user_id = index + 1
        if index < roots:
            parent = None
        elif index < roots * 4:
            parent = rng.randint(1, roots)
        else:
            parent = rng.randint(roots + 1, roots * 4)

        rows.append(
            {
                "id": user_id,
                # Sara Mostafa is index 0 so the catalogue's worked example
                # ("How many deals does Sara Mostafa own?") stays answerable.
                "name": (
                    f"{FIRST_NAMES[index % len(FIRST_NAMES)]} "
                    f"{LAST_NAMES[(index // len(FIRST_NAMES)) % len(LAST_NAMES)]}"
                ),
                "parent_id": parent,
            }
        )
    return rows


def _attribution(rng: random.Random, channel: str) -> dict:
    """Populate only the attribution family the lead actually came from."""

    if channel == "facebook":
        return {
            "facebook_lead_id": rng.randint(10**11, 10**12),
            "facebook_leadgen_id": rng.randint(10**11, 10**12),
            "facebook_ad_id": f"fb-ad-{rng.randint(1000, 9999)}",
            "facebook_ad_account_id": f"act_{rng.randint(10**9, 10**10)}",
            "facebook_ad_account_name": rng.choice(
                ["MarQ Primary", "MarQ Retargeting", "MarQ Brand"]
            ),
            "utm_source": "facebook",
            "utm_medium": "paid_social",
        }
    if channel == "tiktok":
        return {
            "tiktok_lead_id": rng.randint(10**11, 10**12),
            "tiktok_leadgen_id": f"tt-lg-{rng.randint(10000, 99999)}",
            "tiktok_ad_id": f"tt-ad-{rng.randint(1000, 9999)}",
            "tiktok_campaign_id": f"tt-camp-{rng.randint(100, 999)}",
            "tiktok_advertiser_id": f"tt-adv-{rng.randint(100, 999)}",
            "tiktok_adgroup_id": f"tt-grp-{rng.randint(100, 999)}",
            "tiktok_adgroup_name": rng.choice(["Compounds", "Coastal", "Commercial"]),
            "tiktok_campaign_name": rng.choice(["Summer Launch", "Always On"]),
            "tiktok_ad_name": rng.choice(["Video A", "Video B", "Carousel"]),
            "utm_source": "tiktok",
            "utm_medium": "paid_social",
        }
    if channel == "snapchat":
        return {
            "snapchat_lead_id": rng.randint(10**11, 10**12),
            "snapchat_leadgen_id": f"sc-lg-{rng.randint(10000, 99999)}",
            "snapchat_ad_id": f"sc-ad-{rng.randint(1000, 9999)}",
            "snapchat_campaign_id": f"sc-camp-{rng.randint(100, 999)}",
            "snapchat_campaign_name": rng.choice(["Snap Launch", "Snap Retarget"]),
            "snapchat_ad_account_name": "MarQ Snap",
            "snapchat_ad_set_id": f"sc-set-{rng.randint(100, 999)}",
            "snapchat_ad_set_name": rng.choice(["Set A", "Set B"]),
            "utm_source": "snapchat",
            "utm_medium": "paid_social",
        }
    if channel == "organic":
        return {
            "utm_source": rng.choice(["google", "direct", "bayty"]),
            "utm_medium": rng.choice(["organic", "referral"]),
            "utm_campaign": rng.choice([None, "brand", "seo-compounds"]),
        }
    return {}


def build_leads(rng: random.Random, count: int, n_users: int) -> list[dict]:
    rows = []

    for index in range(count):
        lead_id = index + 1

        # Spread over the two years up to today.
        created = Rel(-rng.randint(0, 730), as_timestamp=True)
        deleted = (
            created.shift(rng.randint(1, 200))
            if rng.random() < LEAD_SOFT_DELETE_RATE
            else None
        )

        channel = rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS, k=1)[0]
        stage_id = rng.choice(LEAD_STAGE_IDS)
        agent = rng.randint(1, n_users)
        qualified = rng.random() < 0.42
        converted = rng.random() < 0.09

        # Duplicates are normal. A merged lead points at an earlier survivor,
        # which is what `merged_into_id IS NULL` deduplication relies on.
        merged_into = (
            rng.randint(1, max(1, lead_id - 1))
            if lead_id > 1 and rng.random() < 0.11
            else None
        )

        row = {
            "id": lead_id,
            "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            "tai_id": f"TAI-{100000 + lead_id}",
            "old_crm_id": rng.randint(10**5, 10**6) if rng.random() < 0.3 else None,
            "create_method": rng.choice(CREATED_METHODS),
            "created_at": created,
            "updated_at": created.shift(rng.randint(0, 90)),
            "deleted_at": deleted,
            "first_created_at": created,
            "last_action_at": created.shift(rng.randint(0, 120)),
            # Ownership
            "agent_id": agent,
            "creator_id": rng.randint(1, n_users),
            "team_leader_id": rng.randint(1, max(1, n_users // 4)),
            "franchise_id": rng.randint(1, 12),
            "media_buyer_id": rng.randint(1, n_users) if rng.random() < 0.25 else None,
            "digital_marketing_agent_id": (
                rng.randint(1, n_users) if rng.random() < 0.2 else None
            ),
            "last_assign_at": created.shift(rng.randint(0, 60)),
            "qualified_by": rng.randint(1, n_users) if qualified else None,
            "last_comment_by": rng.randint(1, n_users) if rng.random() < 0.5 else None,
            "google_sheet_synced_by_user_id": None,
            # Funnel
            "lead_stage_id": stage_id,
            "current_stage_entered_at": created.shift(rng.randint(0, 45)),
            "is_stale": rng.random() < 0.22,
            "last_stale_notification_at": (
                created.shift(rng.randint(30, 120)) if rng.random() < 0.15 else None
            ),
            "escalation_level": rng.choices([0, 1, 2], weights=[80, 15, 5], k=1)[0],
            "escalated_at": (
                created.shift(rng.randint(10, 90)) if rng.random() < 0.12 else None
            ),
            # The schema document records this column as 100% NULL and its
            # target table as empty. Kept NULL so a query grouping by it
            # behaves the way it does in production.
            "outcome_reason_id": None,
            "converted_at": created.shift(rng.randint(5, 150)) if converted else None,
            "converted_to_opportunity_id": (
                rng.randint(1, 700) if converted and rng.random() < 0.2 else None
            ),
            "been_new_lead": True,
            # Classification
            "lead_source_id": rng.randint(1, 167),
            "lead_channel_id": rng.randint(1, 52),
            "project_id": rng.randint(1, 60) if rng.random() < 0.7 else None,
            "campaign_id": rng.randint(1, 1296) if rng.random() < 0.45 else None,
            "cold_call_id": (
                rng.randint(1, 500000) if channel == "cold_call" else None
            ),
            # BANT
            "qualification_status": (
                "qualified" if qualified else rng.choice(QUALIFICATION)
            ),
            "qualification_score": rng.randint(0, 100) if qualified else None,
            "budget_status": rng.choice(["unknown", "met", "below", "above"]),
            "authority_status": rng.choice(["unknown", "confirmed", "influencer"]),
            "need_status": rng.choice(["unknown", "confirmed", "exploring"]),
            "timeline_status": rng.choice(
                ["unknown", "immediate", "3_months", "later"]
            ),
            "qualified_at": created.shift(rng.randint(1, 60)) if qualified else None,
            "activity_score_boost": rng.randint(0, 25),
            "engagement_score": rng.randint(0, 100),
            "last_score_decay_at": (
                created.shift(rng.randint(30, 200)) if rng.random() < 0.4 else None
            ),
            "predictive_score": rng.randint(0, 100) if rng.random() < 0.6 else None,
            "potential_review_level": rng.choices(
                [0, 1, 2], weights=[90, 7, 3], k=1
            )[0],
            "potential_review_flagged_at": None,
            "potential_review_for_id": None,
            "potential_review_kept": rng.random() < 0.05,
            # Last-activity cache
            "last_activity_type": rng.choice(ACTIVITY_TYPES),
            "last_activity_status": rng.choice(["done", "missed", "scheduled"]),
            "last_activity_date": created.shift(rng.randint(0, 110)),
            "last_comment_id": rng.randint(1, 10**6) if rng.random() < 0.5 else None,
            "last_comment_text": (
                rng.choice(
                    [
                        "Client asked for a callback next week.",
                        "Not interested in this location.",
                        "Requested floor plans.",
                        "Budget lower than expected.",
                    ]
                )
                if rng.random() < 0.5
                else None
            ),
            "last_comment_at": (
                created.shift(rng.randint(0, 100)) if rng.random() < 0.5 else None
            ),
            # Dedup / replication
            "is_duplicated": merged_into is not None,
            "merged_into_id": merged_into,
            "merged_at": (
                created.shift(rng.randint(1, 90)) if merged_into is not None else None
            ),
            "replicated_from_id": None,
            "is_mobile_normalized": rng.random() < 0.85,
            "lead_session_mobile": f"+2010{rng.randint(10**7, 10**8 - 1)}",
            "google_sheet_sync_uuid": (
                str(uuid.UUID(int=rng.getrandbits(128), version=4))
                if rng.random() < 0.15
                else None
            ),
            "vicidial_id": rng.randint(1, 10**6) if rng.random() < 0.1 else None,
            "is_bayty": rng.random() < 0.08,
            "is_autodialing_enabled": rng.random() < 0.3,
            # SLA & consent
            "first_response_at": created.shift(rng.randint(0, 3)),
            "response_time_minutes": rng.randint(1, 4320),
            "sla_breach_at": (
                created.shift(rng.randint(1, 10)) if rng.random() < 0.18 else None
            ),
            "consent_status": rng.choice(["granted", "unknown", "withdrawn"]),
            "consent_given_at": (
                created.shift(rng.randint(0, 5)) if rng.random() < 0.6 else None
            ),
            "loss_reason_category": rng.choice(LOSS_REASONS),
            # Leads-mart
            "leads_mart_id": rng.randint(1, 10**6) if rng.random() < 0.3 else None,
            "leads_mart_campaign_id": (
                rng.randint(1, 1296) if rng.random() < 0.3 else None
            ),
            "leads_mart_campaign_name": None,
            "leads_mart_project_name": None,
            "leads_mart_integration_id": None,
            "utm_campaign": None,
            "utm_content": None,
            "utm_term": None,
        }

        row.update(_attribution(rng, channel))
        rows.append(row)

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


def build_deals(
    rng: random.Random,
    count: int,
    leads: list[dict],
    n_users: int,
) -> list[dict]:
    """
    Deals reference real leads.

    A lead can produce several deals — production has 4,689 leads with two or
    more — so lead ids are drawn with deliberate repetition rather than
    uniformly. Every lead_id written here exists in `leads`, matching the
    zero orphans the schema document measured.
    """

    live_lead_ids = [row["id"] for row in leads if row["deleted_at"] is None]

    # Roughly one lead in eight that has a deal has more than one.
    lead_pool: list[int] = []
    for lead_id in rng.sample(live_lead_ids, k=min(len(live_lead_ids), count)):
        lead_pool.append(lead_id)
        if rng.random() < 0.13:
            lead_pool.append(lead_id)
        if rng.random() < 0.03:
            lead_pool.append(lead_id)
    rng.shuffle(lead_pool)

    rows = []

    for index in range(count):
        deal_id = index + 1
        created = Rel(-rng.randint(0, 730), as_timestamp=True)
        status = _weighted_status(rng)
        deleted = (
            created.shift(rng.randint(1, 200))
            if rng.random() < DEAL_SOFT_DELETE_RATE
            else None
        )

        # The first few deals deliberately carry no lead — see
        # DEALS_WITHOUT_LEAD.
        if index < DEALS_WITHOUT_LEAD:
            lead_id = None
        else:
            lead_id = lead_pool[index % len(lead_pool)] if lead_pool else None

        # deal_lead_source_id holds a leads.id despite its name, and matches
        # lead_id about 57% of the time.
        if rng.random() < DEAL_HAS_LEAD_SOURCE_RATE:
            if lead_id is not None and rng.random() < DEAL_LEAD_SOURCE_EQUALS_LEAD_RATE:
                deal_lead_source = lead_id
            else:
                deal_lead_source = rng.choice(live_lead_ids)
        else:
            deal_lead_source = None

        contracted = status == "contracted"
        cancelled = status == "cancelled"

        rows.append(
            {
                "id": deal_id,
                "status": status,
                "created_at": created,
                "updated_at": created.shift(rng.randint(0, 120)),
                "deleted_at": deleted,
                "created_method": rng.choice(CREATED_METHODS),
                "batch_date": created.shift(30).as_date(),
                "batch_number": f"B-{2024 + index % 3}-{index % 60:03d}",
                # Ownership
                "agent_id": rng.randint(1, n_users),
                "creator_id": rng.randint(1, n_users),
                "team_leader_id": rng.randint(1, max(1, n_users // 4)),
                "franchise_id": rng.randint(1, 12),
                "owner_id": rng.randint(1, n_users),
                # Origin
                "lead_id": lead_id,
                "opportunity_id": (
                    rng.randint(1, 708)
                    if rng.random() < DEAL_HAS_OPPORTUNITY_RATE
                    else None
                ),
                "deal_source_id": rng.randint(1, 167),
                "deal_lead_source_id": deal_lead_source,
                "last_lead_source_id": rng.randint(1, 167),
                "last_lead_source_at": created.shift(rng.randint(0, 30)),
                "first_verified_lead_source_id": (
                    rng.randint(1, 167) if rng.random() < 0.6 else None
                ),
                "first_verified_lead_source_at": (
                    created.shift(rng.randint(0, 20)) if rng.random() < 0.6 else None
                ),
                "source_resolution_flow": (
                    rng.choice([1, 2, 3])
                    if rng.random() < DEAL_HAS_RESOLUTION_FLOW_RATE
                    else None
                ),
                "lead_occurrence_count": rng.randint(1, 6),
                # Inventory
                "unit_number": f"U-{rng.randint(100, 999)}",
                "project_id": rng.randint(1, 60),
                "developer_id": rng.randint(1, 25),
                "location_id": rng.randint(1, 40),
                "unit_type_id": rng.randint(1, 8),
                "finishing_type_id": rng.randint(1, 5),
                # varchar on purpose — numeric use requires a cast.
                "area": str(rng.randint(75, 480)),
                "selling_type": rng.choices(
                    ["primary", "resale"], weights=[82, 18], k=1
                )[0],
                # float8 holding a year number, not a date.
                "delivery_date": RawSql(
                    f"(EXTRACT(YEAR FROM CURRENT_DATE) + {rng.randint(0, 5)})"
                ),
                "payment_plan": rng.choice(PAYMENT_PLANS),
                # Approvals
                "franchise_owner_approval": rng.choices(
                    ["accepted", "pending", "rejected"], weights=[70, 22, 8], k=1
                )[0],
                "sales_operation_approval": rng.choices(
                    ["accepted", "pending", "rejected"], weights=[65, 27, 8], k=1
                )[0],
                "collection_approval": rng.choices(
                    ["accepted", "pending", "rejected"], weights=[60, 32, 8], k=1
                )[0],
                "collection_amount_status": rng.choices(
                    ["fully_collected", "half_collected", "pending"],
                    weights=[45, 25, 30],
                    k=1,
                )[0],
                "collected_at": (
                    created.shift(rng.randint(40, 120)).as_date()
                    if rng.random() < 0.55
                    else None
                ),
                "is_commercial": rng.random() < 0.18,
                # Dates
                "reservation_date": created.shift(rng.randint(0, 25)),
                "contract_date": (
                    created.shift(rng.randint(20, 130)) if contracted else None
                ),
                "contract_date_added_at": (
                    created.shift(rng.randint(20, 140)) if contracted else None
                ),
                "cancellation_date": (
                    created.shift(rng.randint(10, 200)) if cancelled else None
                ),
                "transaction_date": created.shift(rng.randint(0, 45)),
                # Offsets reach well past `created`, so recent deals produce
                # genuinely upcoming closing dates.
                "expected_closing_date": Rel(created.days + rng.randint(5, 420)),
                # Client PII
                "client_name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                "client_name_ar": (
                    rng.choice(ARABIC_NAMES) if rng.random() < 0.6 else None
                ),
                "national_id": str(rng.randint(10**13, 10**14 - 1)),
                "national_id_address": (
                    rng.choice(CITIES) if rng.random() < 0.5 else None
                ),
                "birth_date": Rel(-rng.randint(8000, 20000)),
                "nationality": rng.choice(NATIONALITIES),
                "social_status": rng.choice(SOCIAL_STATUS),
                "job_title": rng.choice(JOB_TITLES),
                "working_email": (
                    f"client{deal_id}@example.com" if rng.random() < 0.7 else None
                ),
                "living_address": (
                    f"{rng.randint(1, 200)} {rng.choice(CITIES)} St."
                    if rng.random() < 0.6
                    else None
                ),
                "correspondence_address": None,
                "country": "Egypt",
                "city": rng.choice(CITIES),
                # Commission
                "contract_period_id": None,
                "cumulative_sales_at_deal": rng.randint(0, 200) * 1000,
                "has_retroactive_adjustments": rng.random() < 0.1,
                "last_comment": (
                    rng.choice(
                        [
                            "Follow up next week.",
                            "Documents sent to legal.",
                            "Awaiting franchise approval.",
                            "Client requested a unit change.",
                        ]
                    )
                    if rng.random() < 0.45
                    else None
                ),
            }
        )

    return rows


# ============================================================
# Render
# ============================================================


def build_all(scale: int):
    rng = random.Random(SEED)
    n_users = N_USERS * scale
    users = build_users(rng, n_users)
    leads = build_leads(rng, N_LEADS * scale, n_users)
    deals = build_deals(rng, N_DEALS * scale, leads, n_users)
    return users, leads, deals


def render_fixture(scale: int) -> str:
    users, leads, deals = build_all(scale)

    parts = [
        "-- ============================================================",
        "-- MarQ Agent — local test database",
        "--",
        "-- [claude] GENERATED FILE — do not edit by hand.",
        "-- Regenerate with:  python scripts/generate_fixture.py",
        "--",
        "-- Column names come from app/sql/catalogue.py and types from",
        "-- scripts/schema_types.py, transcribed from the MyTAI schema",
        "-- document. The two are checked against each other on every run.",
        "--",
        "-- Masked columns are absent: the catalogue omits them, and this",
        "-- fixture mirrors the agent's permitted surface exactly.",
        "--",
        f"-- TEST DATA ONLY — synthetic, deterministic (seed {SEED}).",
        f"-- {len(users)} users, {len(leads)} leads, {len(deals)} deals.",
        "-- ============================================================",
        "",
        "DROP TABLE IF EXISTS deals;",
        "DROP TABLE IF EXISTS leads;",
        "DROP TABLE IF EXISTS users;",
        "",
    ]

    parts += [f"DROP TYPE IF EXISTS {name};" for name in ENUM_TYPE_NAMES.values()]
    parts += ["", render_enum_types(), ""]

    for table in (USERS_TABLE, LEADS_TABLE, DEALS_TABLE):
        parts += [render_create_table(table), ""]

    tables_and_rows = (
        (USERS_TABLE, users),
        (LEADS_TABLE, leads),
        (DEALS_TABLE, deals),
    )
    for table, rows in tables_and_rows:
        columns = [
            column.name
            for column in table.columns
            if any(column.name in row for row in rows)
        ]
        parts += [
            f"-- {table.name}: {len(rows)} rows, "
            f"{len(columns)}/{len(table.columns)} columns populated",
            insert_stmts(table.name, columns, rows),
        ]

    parts += ["-- Indexes mirroring the documented production access paths.",
              render_indexes(), ""]

    return "\n".join(parts)


def print_report() -> None:
    for table in (USERS_TABLE, LEADS_TABLE, DEALS_TABLE):
        print(f"\n=== {table.name} ({len(table.columns)} columns) ===")
        for column in table.columns:
            print(f"  {column.name:38} {column_type(table.name, column.name)}")


def print_stats(scale: int) -> None:
    from collections import Counter

    users, leads, deals = build_all(scale)

    live_leads = [r for r in leads if r["deleted_at"] is None]
    live_deals = [r for r in deals if r["deleted_at"] is None]
    unique = [r for r in live_leads if r["merged_into_id"] is None]
    lead_counts = Counter(r["lead_id"] for r in deals if r["lead_id"])

    print(f"  users                 {len(users):>6}")
    print(f"  leads                 {len(leads):>6}   live {len(live_leads)}")
    print(f"    unique (unmerged)   {len(unique):>6}")
    print(f"  deals                 {len(deals):>6}   live {len(live_deals)}")
    print(f"    without a lead      {sum(1 for r in deals if r['lead_id'] is None):>6}")
    print(
        f"    leads with 2+ deals "
        f"{sum(1 for c in lead_counts.values() if c > 1):>6}"
    )
    print("\n  live deal status")
    for status, n in Counter(r["status"] for r in live_deals).most_common():
        print(f"    {status:<14} {n:>5}  ({n / len(live_deals):.1%})")
    print("\n  lead stages present   "
          f"{sorted({r['lead_stage_id'] for r in leads})}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="print resolved types")
    parser.add_argument("--stats", action="store_true", help="print distributions")
    parser.add_argument("--scale", type=int, default=1, help="row-count multiplier")
    args = parser.parse_args()

    assert_complete([DEALS_TABLE, LEADS_TABLE, USERS_TABLE])

    if args.report:
        print_report()
        return

    if args.stats:
        print_stats(args.scale)
        return

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(render_fixture(args.scale), encoding="utf-8")
    size = FIXTURE_PATH.stat().st_size / 1024
    print(f"wrote {FIXTURE_PATH.relative_to(REPO_ROOT)} ({size:.0f} KB)")


if __name__ == "__main__":
    main()
