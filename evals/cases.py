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


__all__ = ["CASES", "Case"]
