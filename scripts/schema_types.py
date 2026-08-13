"""
[claude] Column types transcribed from the MyTAI schema document.

Source: `leads-deals-schema.md`, introspected from `information_schema` on
database `mytai`. Every column the catalogue exposes has an entry here.

Why this file exists
--------------------
The fixture generator previously inferred 111 of 169 column types from a
name heuristic (`_id` -> BIGINT, `_at` -> TIMESTAMP, ...) with an override
table for the exceptions. That guessed right most of the time and wrong in
exactly the places that matter: `national_id` and `tai_id` are varchar,
`collected_at` is a date, `facebook_ad_id` is varchar while
`facebook_lead_id` is bigint.

Types are now looked up, never guessed. `assert_complete()` fails if the
catalogue grows a column this file does not know about, so the fixture
cannot silently drift back to guessing.

Masked columns are absent, matching the catalogue.
"""

from __future__ import annotations

# Shorthand used below. Widths are nominal; the fixture only needs the
# storage class to behave like production.
BIG = "BIGINT"
INT = "INTEGER"
SML = "SMALLINT"
STR = "VARCHAR(255)"
TXT = "TEXT"
TS = "TIMESTAMP"
DATE = "DATE"
BOOL = "BOOLEAN"
NUM = "NUMERIC"
F8 = "DOUBLE PRECISION"
UUID = "UUID"


# ============================================================
# leads — 108 columns in production, 104 after masking
# ============================================================

LEADS: dict[str, str] = {
    # Identity & lifecycle
    "id": BIG,
    "name": STR,
    "tai_id": STR,
    "old_crm_id": BIG,
    "create_method": STR,
    "created_at": TS,
    "updated_at": TS,
    "deleted_at": TS,
    "first_created_at": TS,
    "last_action_at": TS,
    # Ownership & routing
    "agent_id": BIG,
    "creator_id": BIG,
    "team_leader_id": BIG,
    "franchise_id": BIG,
    "media_buyer_id": BIG,
    "digital_marketing_agent_id": BIG,
    "last_assign_at": TS,
    "qualified_by": BIG,
    "last_comment_by": BIG,
    "google_sheet_synced_by_user_id": BIG,
    # Funnel position
    "lead_stage_id": BIG,
    "current_stage_entered_at": TS,
    "is_stale": BOOL,
    "last_stale_notification_at": TS,
    "escalation_level": SML,
    "escalated_at": TS,
    "outcome_reason_id": BIG,
    "converted_at": TS,
    "converted_to_opportunity_id": BIG,
    "been_new_lead": BOOL,
    # Classification & interest
    "lead_source_id": BIG,
    "lead_channel_id": BIG,
    "project_id": BIG,
    "campaign_id": BIG,
    "cold_call_id": BIG,
    # Qualification (BANT) & scoring
    "qualification_status": STR,
    "qualification_score": INT,
    "budget_status": STR,
    "authority_status": STR,
    "need_status": STR,
    "timeline_status": STR,
    "qualified_at": TS,
    "activity_score_boost": INT,
    "engagement_score": INT,
    "last_score_decay_at": TS,
    "predictive_score": INT,
    "potential_review_level": SML,
    "potential_review_flagged_at": TS,
    "potential_review_for_id": BIG,
    "potential_review_kept": BOOL,
    # Denormalized last-activity / last-comment cache
    "last_activity_type": STR,
    "last_activity_status": STR,
    "last_activity_date": TS,
    "last_comment_id": BIG,
    "last_comment_text": TXT,
    "last_comment_at": TS,
    # Marketing attribution — Facebook
    "facebook_lead_id": BIG,
    "facebook_leadgen_id": BIG,
    "facebook_ad_id": STR,
    "facebook_ad_account_id": STR,
    "facebook_ad_account_name": STR,
    # Marketing attribution — TikTok
    "tiktok_lead_id": BIG,
    "tiktok_leadgen_id": STR,
    "tiktok_ad_id": STR,
    "tiktok_campaign_id": STR,
    "tiktok_advertiser_id": STR,
    "tiktok_adgroup_id": STR,
    "tiktok_adgroup_name": STR,
    "tiktok_campaign_name": STR,
    "tiktok_ad_name": STR,
    # Marketing attribution — Snapchat
    "snapchat_lead_id": BIG,
    "snapchat_leadgen_id": STR,
    "snapchat_ad_id": STR,
    "snapchat_campaign_id": STR,
    "snapchat_campaign_name": STR,
    "snapchat_ad_account_name": STR,
    "snapchat_ad_set_id": STR,
    "snapchat_ad_set_name": STR,
    # Marketing attribution — UTM & leads-mart
    "utm_source": STR,
    "utm_medium": STR,
    "utm_campaign": STR,
    "utm_content": STR,
    "utm_term": STR,
    "leads_mart_id": INT,
    "leads_mart_campaign_id": INT,
    "leads_mart_campaign_name": STR,
    "leads_mart_project_name": STR,
    "leads_mart_integration_id": BIG,
    # Dedup, merge & replication
    "is_duplicated": BOOL,
    "merged_into_id": BIG,
    "merged_at": TS,
    "replicated_from_id": BIG,
    "is_mobile_normalized": BOOL,
    "lead_session_mobile": STR,
    "google_sheet_sync_uuid": UUID,
    "vicidial_id": BIG,
    "is_bayty": BOOL,
    "is_autodialing_enabled": BOOL,
    # SLA, consent & response
    "first_response_at": TS,
    "response_time_minutes": INT,
    "sla_breach_at": TS,
    "consent_status": STR,
    "consent_given_at": TS,
    "loss_reason_category": STR,
}


# ============================================================
# deals — 69 columns in production, 62 after masking
# ============================================================

DEALS: dict[str, str] = {
    # Identity & lifecycle
    "id": BIG,
    "status": "deals_status_enum",
    "created_at": TS,
    "updated_at": TS,
    "deleted_at": TS,
    "created_method": STR,
    "batch_date": DATE,
    "batch_number": STR,
    # Ownership
    "agent_id": BIG,
    "creator_id": BIG,
    "team_leader_id": BIG,
    "franchise_id": BIG,
    "owner_id": BIG,
    # Origin — lead & opportunity
    "lead_id": BIG,
    "opportunity_id": BIG,
    "deal_source_id": BIG,
    "deal_lead_source_id": BIG,
    "last_lead_source_id": BIG,
    "last_lead_source_at": TS,
    "first_verified_lead_source_id": BIG,
    "first_verified_lead_source_at": TS,
    "source_resolution_flow": SML,
    "lead_occurrence_count": INT,
    # Inventory
    "unit_number": STR,
    "project_id": BIG,
    "developer_id": BIG,
    "location_id": BIG,
    "unit_type_id": BIG,
    "finishing_type_id": BIG,
    "area": STR,
    "selling_type": "deals_selling_type_enum",
    "delivery_date": F8,
    "payment_plan": STR,
    # Approval workflow
    "franchise_owner_approval": "deals_franchise_owner_approval_enum",
    "sales_operation_approval": "deals_sales_operation_approval_enum",
    "collection_approval": "deals_collection_approval_enum",
    "collection_amount_status": "deals_collection_amount_status_enum",
    "collected_at": DATE,
    "is_commercial": BOOL,
    # Dates
    "reservation_date": TS,
    "contract_date": TS,
    "contract_date_added_at": TS,
    "cancellation_date": TS,
    "transaction_date": TS,
    "expected_closing_date": DATE,
    # Client PII
    "client_name": STR,
    "client_name_ar": STR,
    "national_id": STR,
    "national_id_address": STR,
    "birth_date": DATE,
    "nationality": STR,
    "social_status": STR,
    "job_title": STR,
    "working_email": STR,
    "living_address": STR,
    "correspondence_address": STR,
    "country": STR,
    "city": STR,
    # Commission & retro
    "contract_period_id": BIG,
    "cumulative_sales_at_deal": NUM,
    "has_retroactive_adjustments": BOOL,
    # Other
    "last_comment": TXT,
}


# ============================================================
# users — the three columns the catalogue exposes
# ============================================================

USERS: dict[str, str] = {
    "id": BIG,
    "name": STR,
    "parent_id": BIG,
}


TYPES: dict[str, dict[str, str]] = {
    "leads": LEADS,
    "deals": DEALS,
    "users": USERS,
}


# Columns the schema document marks NOT NULL. Everything else is nullable,
# which matters: a fixture where every column is populated hides the
# NULL-handling the agent has to get right.
NOT_NULL: dict[str, set[str]] = {
    "leads": {
        "id",
        "name",
        "is_stale",
        "escalation_level",
        "been_new_lead",
        "qualification_status",
        "budget_status",
        "authority_status",
        "need_status",
        "timeline_status",
        "activity_score_boost",
        "engagement_score",
        "potential_review_level",
        "potential_review_kept",
        "is_duplicated",
        "is_bayty",
        "is_autodialing_enabled",
        "consent_status",
    },
    "deals": {
        "id",
        "status",
        "selling_type",
        "franchise_owner_approval",
        "sales_operation_approval",
        "collection_approval",
        "collection_amount_status",
        "is_commercial",
        "lead_occurrence_count",
        "client_name",
        "has_retroactive_adjustments",
    },
    "users": {"id", "name"},
}


def column_type(table: str, column: str) -> str:
    """Resolve one column's DDL type, including NOT NULL and PK."""

    try:
        base = TYPES[table][column]
    except KeyError as exc:  # pragma: no cover - guarded by assert_complete
        raise KeyError(
            f"No documented type for {table}.{column}. Add it to "
            f"scripts/schema_types.py from leads-deals-schema.md."
        ) from exc

    if column == "id":
        return f"{base} PRIMARY KEY"

    if column in NOT_NULL.get(table, set()):
        return f"{base} NOT NULL"

    return base


def assert_complete(tables) -> None:
    """
    Fail if the catalogue and this file disagree.

    Catches both directions: a catalogue column with no documented type, and
    a documented type for a column the catalogue no longer exposes (usually
    a sign it was masked).
    """

    problems = []

    for table in tables:
        catalogue = {column.name for column in table.columns}
        documented = set(TYPES.get(table.name, {}))

        for missing in sorted(catalogue - documented):
            problems.append(f"{table.name}.{missing}: in catalogue, no type here")
        for extra in sorted(documented - catalogue):
            problems.append(f"{table.name}.{extra}: type here, not in catalogue")

    if problems:
        raise SystemExit(
            "schema_types.py is out of sync with the catalogue:\n  "
            + "\n  ".join(problems)
        )


__all__ = ["NOT_NULL", "TYPES", "assert_complete", "column_type"]
