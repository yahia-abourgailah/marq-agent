"""
[claude] Behavioural cases for the SQL Agent.

Every case targets something the catalogue is *supposed* to teach and that a
model would plausibly get wrong on its own — an invented status, a
misleadingly named column, a varchar that needs casting, a masked field.

Assertions are on properties of the generated SQL, not exact strings, so a
differently-shaped but correct query still passes. Run at temperature 0, so a
failure is a regression rather than an unlucky sample.

    python -m evals.run          # print a report
    pytest -m integration tests/test_prompt_behaviour.py
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    # Which domain's SQL Agent to run the case against.
    domain: str = "deals"
    # Lowercased substrings that must all appear in the generated SQL.
    must_contain: tuple[str, ...] = ()
    # Lowercased substrings that must not appear.
    must_not_contain: tuple[str, ...] = ()
    # True  -> the agent must decline
    # False -> the agent must produce SQL
    expect_refusal: bool = False
    why: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


CASES: tuple[Case, ...] = (
    # ---- status semantics -------------------------------------------
    Case(
        name="active_deals",
        question="How many active deals do we have?",
        must_contain=("count(", "deleted_at is null", "status"),
        must_not_contain=("'active'", "status = 'open'"),
        why="No `active` status exists; it means non-cancelled and non-deleted.",
        tags=("status",),
    ),
    Case(
        name="cancelled_deals",
        question="How many cancelled deals do we have?",
        must_contain=("'cancelled'",),
        why="Cancelled is a status, not a soft delete.",
        tags=("status",),
    ),
    Case(
        name="negotiation_is_not_a_status",
        question="How many deals are in negotiation?",
        expect_refusal=True,
        why="The CRM defines no negotiation status and it must not be guessed.",
        tags=("status", "refusal"),
    ),
    # ---- masked columns ---------------------------------------------
    Case(
        name="masked_contract_price",
        question="What is the total contract price of all deals?",
        expect_refusal=True,
        why="contract_price is masked.",
        tags=("masked", "refusal"),
    ),
    Case(
        name="masked_down_payment",
        question="How much down payment has been collected?",
        expect_refusal=True,
        why="down_payment is masked.",
        tags=("masked", "refusal"),
    ),
    Case(
        name="no_value_column",
        question="What is the total value of all deals?",
        expect_refusal=True,
        why=(
            "deals exposes no monetary value column at all, so the honest "
            "answer is to decline rather than substitute another column."
        ),
        tags=("masked", "refusal"),
    ),
    # ---- column traps -----------------------------------------------
    Case(
        name="area_needs_cast",
        question="Show me the top 5 deals by area.",
        must_contain=("cast(", "limit 5"),
        why="area is varchar; ordering without a cast sorts lexicographically.",
        tags=("types",),
    ),
    Case(
        name="closing_soon_is_upcoming",
        question="Which deals are closing soon?",
        must_contain=("expected_closing_date", "current_date"),
        must_not_contain=("expected_close_date",),
        why="Soon means upcoming, and the column is expected_closing_date.",
        tags=("dates",),
    ),
    Case(
        name="delivery_date_is_a_year",
        question="How many deals are delivering in 2027?",
        must_not_contain=(
            "date_trunc('year', delivery_date)",
            "extract(year from delivery_date)",
        ),
        why="delivery_date is a float8 year number, not a timestamp.",
        tags=("types",),
    ),
    Case(
        name="aging_uses_real_timestamps",
        question="Which deals were created more than 30 days ago?",
        must_contain=("created_at",),
        must_not_contain=("days_in_stage",),
        why=(
            "No days_in_stage column exists; aging must come from the real "
            "timestamp columns."
        ),
        tags=("types",),
    ),
    # ---- relationships ----------------------------------------------
    Case(
        name="owner_name_joins_users",
        question="How many deals does Sara Mostafa own?",
        must_contain=("users", "owner_id", "name"),
        why="Owner names resolve through users, which must be in the prompt.",
        tags=("joins",),
    ),
    Case(
        name="deals_to_leads_via_lead_id",
        question="Show me deals together with the name of the lead they came from.",
        must_contain=("leads", "lead_id"),
        must_not_contain=("opportunities", "lead_sources"),
        why="deals.lead_id is the path; deal_lead_source_id is misleadingly named.",
        tags=("joins",),
    ),
    Case(
        name="unique_leads_deduplicate",
        question="How many unique leads do we have?",
        must_contain=("merged_into_id",),
        why="Leads duplicate; unique demand filters merged_into_id IS NULL.",
        tags=("leads",),
    ),
    # ---- refuse narrowly, not wholesale ------------------------------
    Case(
        name="missing_incidental_field_still_answers",
        question=(
            "Show me the 5 deals closing soonest, with the floor number "
            "and the closing date."
        ),
        must_contain=("expected_closing_date", "limit 5"),
        why=(
            "There is no floor column, but the closing dates are available. "
            "Refusing an answerable question over one missing field is an "
            "over-refusal."
        ),
        tags=("refusal", "shape"),
    ),
    Case(
        name="deal_rows_carry_unit_number",
        question="Show me 5 deals and when they are expected to close.",
        must_contain=("id", "unit_number"),
        why="Deal rows need id plus the human label so they can be looked up.",
        tags=("shape",),
    ),
    # ---- metrics the catalogue defines --------------------------------
    Case(
        name="conversion_counts_leads_not_deals",
        question="What is our overall lead-to-deal conversion rate?",
        must_contain=("exists", "leads"),
        must_not_contain=("count(*) from deals",),
        why=(
            "Conversion counts leads that produced a deal. Dividing total "
            "deals by total leads double-counts any lead with several deals."
        ),
        tags=("metrics",),
    ),
    Case(
        name="rate_keeps_its_denominator",
        question="What is the contract rate for commercial deals?",
        must_contain=("filter", "is_commercial"),
        must_not_contain=("and status = 'contracted'", "and status='contracted'"),
        why=(
            "Putting the measured condition in the WHERE shrinks the "
            "denominator to match the numerator, so the rate is always 100%. "
            "It belongs in a FILTER over the whole population."
        ),
        tags=("metrics",),
    ),
    Case(
        name="group_comparison_is_one_query",
        question=(
            "Are commercial deals more likely to be contracted than "
            "non-commercial ones?"
        ),
        must_contain=("group by", "is_commercial", "filter"),
        why=(
            "One grouped query returns both rates and cannot disagree with "
            "itself; two separate queries can each be wrong independently."
        ),
        tags=("metrics",),
    ),
    Case(
        name="grouped_query_selects_its_key",
        question="What is our lead-to-deal conversion rate by franchise?",
        must_contain=("franchise_id", "group by"),
        why=(
            "Grouping by franchise_id without selecting it returns anonymous "
            "percentages with no way to tell which franchise each belongs to."
        ),
        tags=("shape",),
    ),
    # ---- query shape -------------------------------------------------
    Case(
        name="soft_delete_filter",
        question="List the 10 most recently created deals.",
        must_contain=("deleted_at is null", "limit"),
        why="Soft deletes must be filtered unless deleted rows are asked for.",
        tags=("shape",),
    ),
    Case(
        name="records_carry_an_identifier",
        question="List 5 deals with their unit numbers.",
        must_contain=("id",),
        why="Row results need an identifier to be reportable.",
        tags=("shape",),
    ),
    Case(
        name="grouping_happens_in_sql",
        question="How many deals are there in each status?",
        must_contain=("group by", "count("),
        why="Aggregation belongs in the database, not the model.",
        tags=("shape",),
    ),
)


# ============================================================
# [claude] Leads domain.
#
# Run against the Leads Agent's SQL Agent, which sees leads and users only.
# Its guard rejects `deals`, so a case that reaches for deals data must come
# back as a refusal rather than a query.
# ============================================================

LEADS_CASES: tuple[Case, ...] = (
    Case(
        name="leads_unique_deduplicates",
        domain="leads",
        question="How many unique leads do we have?",
        must_contain=("merged_into_id",),
        why="Leads duplicate; unique demand filters merged_into_id IS NULL.",
        tags=("leads",),
    ),
    Case(
        name="leads_by_stage_groups_by_id",
        domain="leads",
        question="How many leads are in each stage?",
        must_contain=("lead_stage_id", "group by"),
        why=(
            "The stage lookup table is unavailable, but grouping by the id "
            "is still the right answer — it must not refuse outright."
        ),
        tags=("leads",),
    ),
    Case(
        name="leads_named_stage_cannot_resolve",
        domain="leads",
        question="How many leads are in the Hot Case stage?",
        expect_refusal=True,
        why=(
            "Stage names cannot be mapped to ids without the lookup table. "
            "Guessing an id would produce a confident wrong number."
        ),
        tags=("leads", "refusal"),
    ),
    Case(
        name="leads_stale_uses_precomputed_flag",
        domain="leads",
        question="How many stale leads do we have?",
        must_contain=("is_stale",),
        why="is_stale is already computed; staleness is not re-derived.",
        tags=("leads",),
    ),
    Case(
        name="leads_sla_breaches",
        domain="leads",
        question="How many leads breached their SLA?",
        must_contain=("sla_breach_at",),
        why="sla_breach_at is non-NULL only when the SLA was breached.",
        tags=("leads",),
    ),
    Case(
        name="leads_by_utm_source_can_use_names",
        domain="leads",
        question="Which utm sources bring the most leads?",
        must_contain=("utm_source", "group by"),
        why="utm_source is real text, unlike the id-only lookups.",
        tags=("leads",),
    ),
    Case(
        name="leads_conversion_uses_converted_at",
        domain="leads",
        question="How many leads have converted?",
        must_contain=("converted",),
        why="Conversion is recorded on the lead, not by joining deals.",
        tags=("leads",),
    ),
    Case(
        name="leads_outcome_reason_is_all_null",
        domain="leads",
        question="What are the top loss reasons for our leads?",
        must_not_contain=("outcome_reason_id",),
        why=(
            "outcome_reason_id is NULL for every row; loss_reason_category "
            "is the populated column."
        ),
        tags=("leads",),
    ),
    Case(
        name="leads_agent_cannot_reach_deals",
        domain="leads",
        question="How many contracted deals do we have?",
        expect_refusal=True,
        why=(
            "The Leads domain does not include deals. Its guard would reject "
            "the query, so the agent must decline rather than write it."
        ),
        tags=("leads", "refusal", "isolation"),
    ),
    Case(
        name="leads_response_time_is_precomputed",
        domain="leads",
        question="What is the average response time for leads?",
        must_contain=("response_time_minutes", "avg("),
        why="response_time_minutes is already computed from first_response_at.",
        tags=("leads",),
    ),
)


CASES = CASES + LEADS_CASES


__all__ = ["CASES", "LEADS_CASES", "Case"]
