"""
The CRM schema contract exposed to the SQL Agent.

Columns, business rules, relationships and enums for the tables the agent may
query. Masked columns are deliberately absent: omitting them here is how the
agent is kept from treating them as queryable fields.

This catalogue is also the source of the SQLGuard table allowlist, so the
schema the agent sees and the surface the guard permits cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    name: str
    description: str


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    columns: tuple[Column, ...]


# ============================================================
# DEALS
# ============================================================

DEALS_TABLE = Table(
    name="deals",
    description=(
        "CRM deal records representing EOI, reservation, contracted, "
        "and cancelled deals."
    ),
    columns=(
        # Identity / lifecycle
        Column("id", "Unique deal identifier."),
        Column(
            "status",
            (
                "Deal lifecycle status. Allowed values are: "
                "cancelled, eoi, contracted, reservation."
            ),
        ),
        Column("created_at", "Timestamp when the deal record was created."),
        Column("updated_at", "Timestamp when the deal record was last updated."),
        Column(
            "deleted_at",
            "Soft-delete timestamp. Live records have deleted_at IS NULL.",
        ),
        Column("created_method", "Method by which the deal was created."),
        Column("batch_date", "Deal batch date."),
        Column("batch_number", "Deal batch number."),

        # Ownership
        # [claude] This is the owner, confirmed against the live MyTAI
        # schema: "agent_id — deal owner — scope axis together with
        # deal_percentages", and MyDealsScope filters on `agent_id = :me`.
        #
        # Both this and owner_id below used to describe themselves as the
        # owner, so the SQL Agent joined on whichever it picked — 13 deals
        # for one person through owner_id against 6 through agent_id, the
        # columns disagreeing on 307 of 350 fixture rows. I first resolved
        # that the wrong way round, making owner_id canonical because an
        # existing eval expected it. The eval was wrong too.
        Column(
            "agent_id",
            (
                "The deal's owner. This is the column that answers who owns "
                "a deal, whose deals they are, and 'my deals'."
            ),
        ),
        Column("creator_id", "User ID that originally created the deal."),
        Column("team_leader_id", "Team leader user ID."),
        Column("franchise_id", "Franchise / branch ID."),
        Column(
            "owner_id",
            (
                "A user reference on the deal. Despite the name this is NOT "
                "the ownership axis — use agent_id for who owns a deal."
            ),
        ),

        # Lead / opportunity / attribution
        Column(
            "lead_id",
            "Related lead ID. Joins to leads.id. This is the primary "
            "relationship between deals and leads.",
        ),
        Column(
            "opportunity_id",
            "Related opportunity ID. Joins to opportunities.id.",
        ),
        Column(
            "deal_source_id",
            "Deal source ID. Joins to lead_sources.id.",
        ),
        Column(
            "deal_lead_source_id",
            (
                "Misleadingly named field that contains a leads.id, "
                "not a lead_sources.id."
            ),
        ),
        Column(
            "last_lead_source_id",
            "Last lead source ID. Joins to lead_sources.id.",
        ),
        Column(
            "last_lead_source_at",
            "Timestamp when the last lead source was recorded.",
        ),
        Column(
            "first_verified_lead_source_id",
            "First verified lead source ID.",
        ),
        Column(
            "first_verified_lead_source_at",
            "Timestamp when the first verified lead source was recorded.",
        ),
        Column(
            "source_resolution_flow",
            "Attribution resolution flow. Known values are 1, 2, or 3.",
        ),
        Column(
            "lead_occurrence_count",
            "Number of lead occurrences associated with the deal.",
        ),

        # Inventory
        Column("unit_number", "Unit number."),
        Column("project_id", "Project ID."),
        Column("developer_id", "Developer ID."),
        Column("location_id", "Location ID."),
        Column("unit_type_id", "Unit type ID."),
        Column("finishing_type_id", "Finishing type ID."),
        Column(
            "area",
            "Unit area stored as varchar in the CRM.",
        ),
        Column(
            "selling_type",
            "Selling type. Allowed values are primary or resale.",
        ),
        Column(
            "delivery_date",
            (
                "Delivery year stored as a double precision number, "
                "not a timestamp."
            ),
        ),
        Column("payment_plan", "Payment plan description."),

        # Approval workflow
        Column(
            "franchise_owner_approval",
            "Franchise-owner approval: pending, accepted, rejected.",
        ),
        Column(
            "sales_operation_approval",
            "Sales-operation approval: pending, accepted, rejected.",
        ),
        Column(
            "collection_approval",
            "Collection approval: pending, accepted, rejected.",
        ),
        Column(
            "collection_amount_status",
            (
                "Collection status: pending, half_collected, "
                "fully_collected."
            ),
        ),
        Column("collected_at", "Date when collection was completed."),
        Column(
            "is_commercial",
            (
                "Whether the deal is classified as commercial. "
                "Used by the commercial deal visibility scope."
            ),
        ),

        # Dates
        Column(
            "reservation_date",
            "Reservation milestone timestamp.",
        ),
        Column(
            "contract_date",
            "Contract milestone timestamp.",
        ),
        Column(
            "contract_date_added_at",
            "Timestamp when contract_date was added.",
        ),
        Column(
            "cancellation_date",
            "Cancellation milestone timestamp.",
        ),
        Column(
            "transaction_date",
            (
                "Business/reporting timestamp used by CRM scopes "
                "and reports."
            ),
        ),
        Column(
            "expected_closing_date",
            "Expected closing date.",
        ),

        # Client information
        Column("client_name", "Client name. PII."),
        Column("client_name_ar", "Client name in Arabic."),
        Column("national_id", "Client national ID. PII."),
        Column(
            "national_id_address",
            "Address associated with the national ID. PII.",
        ),
        Column("birth_date", "Client birth date. PII."),
        Column("nationality", "Client nationality."),
        Column("social_status", "Client social status."),
        Column("job_title", "Client job title."),
        Column("working_email", "Client work email. PII."),
        Column("living_address", "Client living address. PII."),
        Column(
            "correspondence_address",
            "Client correspondence address. PII.",
        ),
        Column("country", "Client country."),
        Column("city", "Client city."),

        # Commission / retro
        Column(
            "contract_period_id",
            "Commission contract period ID.",
        ),
        Column(
            "cumulative_sales_at_deal",
            "Cumulative sales at the time of the deal.",
        ),
        Column(
            "has_retroactive_adjustments",
            "Whether the deal has retroactive adjustments.",
        ),

        # Other
        Column(
            "last_comment",
            "Latest deal comment.",
        ),
    ),
)


# ============================================================
# LEADS
# ============================================================

LEADS_TABLE = Table(
    name="leads",
    description=(
        "CRM lead records. Deals relate directly to leads through "
        "deals.lead_id = leads.id."
    ),
    columns=(
        # Identity / lifecycle
        Column("id", "Unique lead identifier."),
        Column("name", "Lead display name. Not guaranteed unique."),
        Column("tai_id", "TAI identifier."),
        Column("old_crm_id", "Legacy CRM identifier."),
        Column("create_method", "Method by which the lead was created."),
        Column("created_at", "Timestamp when the lead was created."),
        Column("updated_at", "Timestamp when the lead was updated."),
        Column(
            "deleted_at",
            "Soft-delete timestamp. Live records have deleted_at IS NULL.",
        ),
        Column("first_created_at", "Original creation timestamp."),
        Column("last_action_at", "Timestamp of the last action."),

        # Ownership / routing
        Column("agent_id", "Current lead owner user ID."),
        Column("creator_id", "Original lead creator user ID."),
        Column("team_leader_id", "Team leader user ID."),
        Column("franchise_id", "Franchise / branch ID."),
        Column("media_buyer_id", "Media buyer user ID."),
        Column(
            "digital_marketing_agent_id",
            "Digital marketing agent user ID.",
        ),
        Column("last_assign_at", "Timestamp of the last assignment."),
        Column("qualified_by", "User ID that qualified the lead."),
        Column(
            "last_comment_by",
            "User ID that created the last comment.",
        ),
        Column(
            "google_sheet_synced_by_user_id",
            "User ID that synchronized the lead with Google Sheets.",
        ),

        # Funnel
        Column("lead_stage_id", "Lead stage ID."),
        Column(
            "current_stage_entered_at",
            "Timestamp when the lead entered its current stage.",
        ),
        Column(
            "is_stale",
            "Whether the lead is stale according to its stage SLA.",
        ),
        Column(
            "last_stale_notification_at",
            "Timestamp of the last stale notification.",
        ),
        Column("escalation_level", "Current escalation level."),
        Column("escalated_at", "Timestamp when the lead was escalated."),
        Column("outcome_reason_id", "Lead outcome reason ID."),
        Column("converted_at", "Timestamp when the lead converted."),
        Column(
            "converted_to_opportunity_id",
            "Opportunity created from the lead.",
        ),
        Column("been_new_lead", "Whether the lead has been in New Lead."),

        # Classification
        Column("lead_source_id", "Lead source ID."),
        Column("lead_channel_id", "Lead channel ID."),
        Column("project_id", "Project ID."),
        Column("campaign_id", "Campaign ID."),
        Column("cold_call_id", "Cold call ID."),

        # Qualification
        Column("qualification_status", "Lead qualification status."),
        Column("qualification_score", "Lead qualification score."),
        Column("budget_status", "BANT budget status."),
        Column("authority_status", "BANT authority status."),
        Column("need_status", "BANT need status."),
        Column("timeline_status", "BANT timeline status."),
        Column("qualified_at", "Timestamp when the lead was qualified."),
        Column("activity_score_boost", "Activity score boost."),
        Column("engagement_score", "Lead engagement score."),
        Column(
            "last_score_decay_at",
            "Timestamp of the last score decay.",
        ),
        Column("predictive_score", "Predictive lead score."),
        Column("potential_review_level", "Potential review level."),
        Column(
            "potential_review_flagged_at",
            "Timestamp when potential review was flagged.",
        ),
        Column(
            "potential_review_for_id",
            "User/process identifier associated with potential review.",
        ),
        Column(
            "potential_review_kept",
            "Whether the potential review was kept.",
        ),

        # Last activity / comments
        Column(
            "last_activity_type",
            "Type of the latest activity.",
        ),
        Column(
            "last_activity_status",
            "Status of the latest activity.",
        ),
        Column(
            "last_activity_date",
            "Timestamp of the latest activity.",
        ),
        Column("last_comment_id", "Latest comment ID."),
        Column("last_comment_text", "Latest comment text."),
        Column("last_comment_at", "Timestamp of the latest comment."),

        # Facebook attribution
        Column("facebook_lead_id", "Facebook lead ID."),
        Column("facebook_leadgen_id", "Facebook leadgen ID."),
        Column("facebook_ad_id", "Facebook ad ID."),
        Column("facebook_ad_account_id", "Facebook ad account ID."),
        Column("facebook_ad_account_name", "Facebook ad account name."),

        # TikTok attribution
        Column("tiktok_lead_id", "TikTok lead ID."),
        Column("tiktok_leadgen_id", "TikTok leadgen ID."),
        Column("tiktok_ad_id", "TikTok ad ID."),
        Column("tiktok_campaign_id", "TikTok campaign ID."),
        Column("tiktok_advertiser_id", "TikTok advertiser ID."),
        Column("tiktok_adgroup_id", "TikTok ad group ID."),
        Column("tiktok_adgroup_name", "TikTok ad group name."),
        Column("tiktok_campaign_name", "TikTok campaign name."),
        Column("tiktok_ad_name", "TikTok ad name."),

        # Snapchat attribution
        Column("snapchat_lead_id", "Snapchat lead ID."),
        Column("snapchat_leadgen_id", "Snapchat leadgen ID."),
        Column("snapchat_ad_id", "Snapchat ad ID."),
        Column("snapchat_campaign_id", "Snapchat campaign ID."),
        Column("snapchat_campaign_name", "Snapchat campaign name."),
        Column(
            "snapchat_ad_account_name",
            "Snapchat ad account name.",
        ),
        Column("snapchat_ad_set_id", "Snapchat ad set ID."),
        Column("snapchat_ad_set_name", "Snapchat ad set name."),

        # UTM / leads mart
        Column("utm_source", "UTM source."),
        Column("utm_medium", "UTM medium."),
        Column("utm_campaign", "UTM campaign."),
        Column("utm_content", "UTM content."),
        Column("utm_term", "UTM term."),
        Column("leads_mart_id", "Leads mart ID."),
        Column("leads_mart_campaign_id", "Leads mart campaign ID."),
        Column(
            "leads_mart_campaign_name",
            "Leads mart campaign name.",
        ),
        Column(
            "leads_mart_project_name",
            "Leads mart project name.",
        ),
        Column(
            "leads_mart_integration_id",
            "Leads mart integration ID.",
        ),

        # Dedup / merge / replication
        Column("is_duplicated", "Whether the lead is duplicated."),
        Column(
            "merged_into_id",
            "Lead ID of the deduplication survivor.",
        ),
        Column("merged_at", "Timestamp when the lead was merged."),
        Column("replicated_from_id", "Source lead ID for replication."),
        Column(
            "is_mobile_normalized",
            "Whether the lead mobile was normalized.",
        ),
        Column("lead_session_mobile", "Lead session mobile."),
        Column(
            "google_sheet_sync_uuid",
            "Google Sheets synchronization UUID.",
        ),
        Column("vicidial_id", "Vicidial ID."),
        Column("is_bayty", "Whether the lead is a Bayty lead."),
        Column(
            "is_autodialing_enabled",
            "Whether autodialing is enabled.",
        ),

        # SLA / consent
        Column(
            "first_response_at",
            "Timestamp of the first response.",
        ),
        Column(
            "response_time_minutes",
            "Response time in minutes.",
        ),
        Column("sla_breach_at", "Timestamp of SLA breach."),
        Column("consent_status", "Consent status."),
        Column("consent_given_at", "Timestamp when consent was given."),
        Column(
            "loss_reason_category",
            "Lead loss reason category.",
        ),
    ),
)


# ============================================================
# USERS
# ============================================================

USERS_TABLE = Table(
    name="users",
    description=(
        "CRM users. Used to resolve deal and lead ownership IDs "
        "to user names and reporting relationships."
    ),
    columns=(
        Column("id", "Unique user identifier."),
        Column("name", "User display name."),
        Column(
            "parent_id",
            (
                "Reporting hierarchy parent user ID. Used by CRM "
                "visibility scopes."
            ),
        ),
    ),
)


# ============================================================
# RELATIONSHIPS
# ============================================================

DEALS_RELATIONSHIPS: tuple[str, ...] = (
    "deals.lead_id -> leads.id",
    "deals.opportunity_id -> opportunities.id",
    "deals.deal_source_id -> lead_sources.id",
    "deals.last_lead_source_id -> lead_sources.id",
    "deals.owner_id -> users.id",
    "deals.agent_id -> users.id",
    "deals.creator_id -> users.id",
    "deals.team_leader_id -> users.id",
    "deals.project_id -> projects.id",
    "deals.developer_id -> developers.id",
    "deals.location_id -> locations.id",
    "deals.unit_type_id -> unit_types.id",
    "deals.finishing_type_id -> finishing_types.id",
    "deals.contract_period_id -> commission_contract_periods.id",
)


LEADS_RELATIONSHIPS: tuple[str, ...] = (
    "leads.agent_id -> users.id",
    "leads.creator_id -> users.id",
    "leads.team_leader_id -> users.id",
    "leads.franchise_id -> franchises.id",
    "leads.lead_stage_id -> lead_stages.id",
    "leads.lead_source_id -> lead_sources.id",
    "leads.lead_channel_id -> lead_channels.id",
    "leads.project_id -> projects.id",
    "leads.campaign_id -> campaigns.id",
    "leads.cold_call_id -> cold_calls.id",
    "leads.converted_to_opportunity_id -> opportunities.id",
    "leads.merged_into_id -> leads.id",
)


# ============================================================
# BUSINESS RULES
# ============================================================

# ============================================================
# BUSINESS RULES
# ============================================================
#
# [claude] Rewritten and split by scope.
#
# The previous block was 43 numbered rules in one 2,600-token string, and
# most of it was not teaching the model anything:
#
#   - Eight rules restated the four status values that the ENUMS block
#     already lists ("EOI means status = \'eoi\'").
#   - The masked-column instruction appeared three times, once at ~350
#     words.
#   - Four rules forbade INSERT/UPDATE/DELETE/DDL, which SQLGuard rejects
#     outright — the model cannot execute anything.
#   - Rules listed date columns, client columns and enum values that the
#     SCHEMA block already spells out directly above them.
#   - Several taught plain SQL ("use COUNT, AVG, MIN, MAX", "use IS NULL
#     correctly"), which the model already knows.
#
# What is left is the non-obvious part: the traps a competent SQL writer
# would fall into without being told. Each rule now earns its tokens.
#
# Splitting by scope means a caller can send only the rules for the tables
# in play — see build_rules(). Ordering is deliberate: the rules most often
# violated come first, because rule 29 of 43 ("consider merged_into_id")
# was being ignored in practice.


GENERIC_RULES = """\
- Use only the tables and columns in SCHEMA. Never invent a table, column,
  enum value or relationship, and never substitute a similar-sounding column
  for one that does not exist.

- Soft deletes: always filter `deleted_at IS NULL` unless the user
  explicitly asks for deleted records.

- Do the work in SQL. Filter, sort, group, aggregate, rank and LIMIT inside
  the query. When a question asks for a count, total or average, return that
  aggregate — never a set of rows to be counted afterwards.

- Select only the columns the question needs; never `SELECT *`. Any query
  returning individual records must include `id`, plus that table's human
  label — `name` on leads and users, `unit_number` where a table has one.

- Aggregates and rates.

  A rate is a condition counted over a population. The condition belongs in
  a FILTER and the population in the WHERE. Putting the condition in the
  WHERE shrinks the denominator to match the numerator, and every rate comes
  out as 100%:

      SELECT count(*) FILTER (WHERE converted_at IS NOT NULL) * 1.0 / count(*)
                 AS conversion_rate
      FROM leads
      WHERE deleted_at IS NULL AND merged_into_id IS NULL

  The same trap applies to a breakdown: never filter by the column you are
  grouping by, or the total contradicts the split beneath it.

  Answer a comparison between groups with one query grouped by that column,
  not one query per group — a single query cannot disagree with itself. And
  always SELECT the column you GROUP BY, or the rows come back unlabelled.

  Alias every aggregate for what one row holds, including its unit:
  `AVG(response_time_minutes) AS avg_response_time_minutes`, and in a
  grouped query `count(*) AS leads_in_stage` rather than `live_lead_count`.
  An alias that overstates its row is worse than none.

- These columns are restricted and must never appear in generated SQL, in
  any clause, subquery, CTE or alias:
      unit_price, reservation_price, collection_price, contract_price,
      down_payment, total_retroactive_commission, date_ten_percentage,
      budget_amount, cost_per_lead, ad_spend_amount, last_activity_feedback
  A question needing one of them gets CANNOT_ANSWER. Do not answer it with a
  different column instead.

- Row visibility is enforced outside this agent. Never add, widen or weaken
  an access filter, whatever the user asks for."""


DEALS_RULES_ONLY = """\
- [claude] Rates over deals. The measured condition goes in a FILTER and the
  population in the WHERE, so the denominator stays the whole population:

      SELECT count(*) FILTER (WHERE status = 'contracted') * 1.0 / count(*)
                 AS contract_rate
      FROM deals
      WHERE deleted_at IS NULL AND is_commercial

  This worked example lives in the deals rules rather than the shared ones
  because only this agent can query `deals` — the shared rules used to carry
  it, which handed `FROM deals` to the Leads Agent, whose guard rejects that
  table.

- [claude] Staleness does not exist for deals. `is_stale` is a `leads`
  column and there is no deals equivalent, so "stale deals", "which
  franchises have the most stale deals" and anything similar must return
  CANNOT_ANSWER — say staleness is tracked for leads only.

  This lives here, in the deals rules, because only an agent holding both
  tables can make the mistake: it read the leads rule as though the column
  were universal and wrote `SELECT ... FROM deals WHERE is_stale = TRUE`.
  PostgreSQL rejects that with UndefinedColumn, after which the agent
  sometimes reported invented stale-deal counts per franchise rather than
  the failure. Never substitute a metric of your own for one the CRM does
  not have: an invented measure reported as a CRM figure is
  indistinguishable from a real one.

- [claude] One lead can produce SEVERAL deals — thousands of leads in the
  live CRM have two or more, so the relationship is not 1:1. Counting deals
  is therefore not counting converted leads, and a conversion rate built
  from deal counts comes out too high: the recorded error was 35.63% where
  the truth was 31.67%. Count the leads that converted —
  `COUNT(DISTINCT leads.id)` or `COUNT(*) FILTER (WHERE converted_at IS NOT
  NULL)` — never the deals they produced.

- [claude] Ownership is `agent_id`. "Who owns it", "whose deals", "X's
  deals" and "my deals" all resolve through `deals.agent_id = users.id`.
  This is the axis the CRM's own row-level visibility uses, so a count made
  any other way will disagree with what the user sees on screen.

  `owner_id` is also a users reference and is a different person on most
  rows — the two disagree on 307 of 350 deals in the test fixture. Picking
  the wrong one does not fail; it returns a different number with equal
  confidence. Do not use `owner_id` for ownership questions.

- `status` is one of: cancelled, eoi, contracted, reservation. Nothing else
  exists.
      active         -> status IN ('eoi', 'reservation', 'contracted')
      won / closed   -> status = 'contracted'
      cancelled      -> status = 'cancelled'
  Cancelled deals still have `deleted_at IS NULL`, so identify them by
  status, never by soft delete. Any other status the user names — such as
  negotiation, open or pending — does not exist: return CANNOT_ANSWER rather
  than guessing a mapping or using `status NOT IN (...)`.

- `transaction_date` is the business date CRM reports use. `created_at` is
  only when the record was entered. Prefer transaction_date for questions
  about business performance in a period.

- Closing soon means upcoming:
      WHERE expected_closing_date >= CURRENT_DATE
      ORDER BY expected_closing_date ASC
  Without the CURRENT_DATE filter the earliest rows are deals that closed
  years ago. Drop it only when the user asks about past or overdue dates.
  There is no `expected_close_date` column.

- `area` is varchar. Always `CAST(area AS numeric)` before ordering or
  comparing it — otherwise '97' sorts above '446'.

- `delivery_date` is a double precision year number such as 2027, not a
  timestamp. Compare it numerically; never apply date functions to it.

- `deals.lead_id = leads.id` is the only path from a deal to its lead. One
  lead can have many deals. `deal_lead_source_id` holds a `leads.id` despite
  its name — never join it to a source table.

- Lead-to-deal conversion means: of the leads in scope, how many produced at
  least one deal. Count leads, not deals — a lead with three deals would
  otherwise inflate the numerator. Both tables need their soft-delete filter:

      SELECT COUNT(*) FILTER (WHERE EXISTS (
                 SELECT 1 FROM deals d
                 WHERE d.lead_id = leads.id AND d.deleted_at IS NULL
             )) * 1.0 / COUNT(*) AS conversion_rate
      FROM leads
      WHERE leads.deleted_at IS NULL

  Add `GROUP BY leads.franchise_id` (or lead_source_id, agent_id) to break it
  down; the `WHERE leads.deleted_at IS NULL` stays in every variant.

  This is a different measure from `leads.converted_at`, which is the CRM's
  own conversion flag and does not require a deal to exist.

- `owner_id`, `agent_id`, `creator_id` and `team_leader_id` all reference
  `users.id`. Resolve person names by joining users and filtering
  `users.name`.

- `franchise_id`, `project_id`, `developer_id`, `location_id`,
  `unit_type_id` and `finishing_type_id` are ids whose lookup tables are not
  in SCHEMA. Group, count, order and filter by the id freely — "which
  franchise has the most deals" is `GROUP BY franchise_id ORDER BY count(*)
  DESC`. What is unavailable is the mapping between one of these ids and its
  name, in either direction. Report the id; never invent the name.

- deals has no monetary, price or currency column, and no `days_in_stage`.
  Derive age from the timestamp columns in SCHEMA. If a question needs an
  amount, return CANNOT_ANSWER."""


LEADS_RULES_ONLY = """\
- Leads legitimately duplicate. When the user asks about unique leads,
  unique demand, or distinct people, you MUST filter
  `merged_into_id IS NULL`. `COUNT(DISTINCT id)` is not deduplication — it
  counts merged duplicates as separate leads. `is_duplicated` marks a lead
  that was merged away, not one that has duplicates.

- `lead_stage_id` is a numeric id and its lookup table is not in SCHEMA.
  Group, count, order and filter by the id freely — "leads per stage" is
  `GROUP BY lead_stage_id`. What is unavailable is the mapping between a
  stage's name and its id, in either direction. Valid ids are 1-14, 16 and
  17; there is no stage 15.

- `lead_source_id`, `lead_channel_id`, `campaign_id` and `project_id` behave
  the same way: the id is queryable, the name is not, and must never be
  invented. `utm_source` and `utm_medium` are plain text on the lead itself
  and can be grouped and filtered by name.

- `outcome_reason_id` is NULL for every row in this CRM. Never filter,
  group or join on it — a query using it returns nothing and looks like a
  real zero. Loss and outcome reasons live in `loss_reason_category`, which
  is populated text; use that instead.

- Conversion is recorded on the lead: `converted_at` is when it converted
  and `converted_to_opportunity_id` points at the opportunity. Use those for
  conversion questions.

  [claude] Count the leads that converted —
  `COUNT(*) FILTER (WHERE converted_at IS NOT NULL)` — as the numerator, over
  all live leads as the denominator.

- `is_stale` is already computed; do not recalculate staleness from dates.
  `current_stage_entered_at` is the stage dwell clock — time in the current
  stage is measured from it, not from `created_at`.

  `is_stale` belongs to `leads` and to no other table.

- `response_time_minutes` is already computed from `first_response_at`. Use
  it directly. `sla_breach_at` is non-NULL only when the SLA was breached,
  so `sla_breach_at IS NOT NULL` counts breaches.

- [claude] "Does responding faster convert better", "conversion rate by
  response speed" and anything else splitting conversion by a numeric column
  is answerable, and has three ways to go wrong at once. Bucket the number
  yourself with CASE, SELECT the bucket, and take the rate inside each one:

      SELECT
        CASE WHEN response_time_minutes <= 60 THEN 'within_1_hour'
             ELSE 'over_1_hour' END AS response_bucket,
        COUNT(*) AS leads_in_bucket,
        COUNT(converted_at) AS converted_in_bucket,
        ROUND(100.0 * COUNT(converted_at) / COUNT(*), 2) AS conversion_rate_pct
      FROM leads
      WHERE deleted_at IS NULL AND response_time_minutes IS NOT NULL
      GROUP BY response_bucket

  The three traps, all of which produce a confident wrong number:

    the denominator is the leads in that bucket, `COUNT(*)`, never the
    converted leads overall — dividing by the total converted gives each
    bucket's share of conversions, which sums to 100% across buckets and is
    a different question;

    the bucket expression must appear in SELECT, or the rates come back
    unlabelled and cannot be attributed to fast or slow;

    rows with a NULL `response_time_minutes` were never responded to and
    belong in neither bucket, so exclude them rather than letting them fall
    into the slow one.

  A bucket holding very few leads makes its rate unstable, which is why
  `leads_in_bucket` is selected too. Never report a rate without it.

- Which date: `created_at` is when the record was entered,
  `first_created_at` the original creation before any replication, and
  `last_action_at` the most recent activity of any kind.

- Qualification is BANT: `qualification_status` is the overall verdict and
  `budget_status`, `authority_status`, `need_status`, `timeline_status` are
  the four axes. `qualification_score`, `engagement_score` and
  `predictive_score` are separate numeric scores — do not treat them as
  interchangeable."""


USERS_RULES_ONLY = """\
- `users.parent_id` is the reporting tree. Never apply hierarchy filtering
  yourself; visibility is handled outside this agent."""


RULES_BY_TABLE = {
    "deals": DEALS_RULES_ONLY,
    "leads": LEADS_RULES_ONLY,
    "users": USERS_RULES_ONLY,
}


def render_enums() -> str:
    """
    [claude] Render the enum map as lines.

    It was interpolated as a Python dict, so the model received
    `{'status': ('cancelled', 'eoi', ...), ...}` — quotes, braces and commas
    spending tokens to say nothing.
    """

    return "\n".join(
        f"  {column}: {', '.join(values)}"
        for column, values in DEALS_ENUMS.items()
    )


def relationships_for(table_names: Sequence[str] | None = None) -> tuple[str, ...]:
    """
    [claude] Return only the relationships whose both ends are queryable.

    The full lists document joins to tables that are not in the catalogue —
    projects, opportunities, lead_sources and a dozen others. Sending those
    to the SQL Agent invites it to write joins SQLGuard then rejects, which
    costs a retry and produces a confusing "table is not allowed" error for
    a relationship the prompt itself advertised.

    Filtering by what is actually available keeps the prompt honest, and
    shrinks it. When a lookup table is added to the catalogue, its joins
    start appearing automatically.
    """

    if table_names is None:
        table_names = tuple(RULES_BY_TABLE)

    available = {name.lower() for name in table_names}

    def reachable(relationship: str) -> bool:
        source, _, target = relationship.partition(" -> ")
        return (
            source.split(".")[0].strip().lower() in available
            and target.split(".")[0].strip().lower() in available
        )

    return tuple(
        relationship
        for relationship in DEALS_RELATIONSHIPS + LEADS_RELATIONSHIPS
        if reachable(relationship)
    )


def build_rules(table_names: Sequence[str] | None = None) -> str:
    """
    [claude] Compose the rules block for a set of tables.

    Generic rules always apply. Table-specific rules are included only for
    the tables in play, so narrowing the schema also narrows the rules.
    """

    if table_names is None:
        table_names = tuple(RULES_BY_TABLE)

    sections = [GENERIC_RULES]
    sections += [
        RULES_BY_TABLE[name]
        for name in table_names
        if name in RULES_BY_TABLE
    ]

    return "\n\n".join(sections)


# Every rule set, for callers that do not narrow by table.
ALL_RULES = build_rules()


# ============================================================
# ENUMS
# ============================================================

DEALS_ENUMS: dict[str, tuple[str, ...]] = {
    "status": (
        "cancelled",
        "eoi",
        "contracted",
        "reservation",
    ),
    "selling_type": (
        "primary",
        "resale",
    ),
    "franchise_owner_approval": (
        "pending",
        "accepted",
        "rejected",
    ),
    "sales_operation_approval": (
        "pending",
        "accepted",
        "rejected",
    ),
    "collection_approval": (
        "pending",
        "accepted",
        "rejected",
    ),
    "collection_amount_status": (
        "pending",
        "half_collected",
        "fully_collected",
    ),
}


# ============================================================
# PROMPT RENDERING
# ============================================================
#
# [claude] The prompt used to interpolate the Table dataclass directly, so
# what reached the model was its Python repr:
#
#     Table(name='deals', description='...', columns=(Column(name='id',
#     description='Unique deal identifier.'), Column(name='status', ...
#
# The `Column(name=`/`description=`/`)` scaffolding repeats once per column
# and carries no meaning for the model — 30% of that block was punctuation.
# Rendering the same information as plain lines costs nothing in fidelity
# and is what makes it affordable to send `leads` and `users` too, which the
# prompt never included even though the business rules tell the model to
# join them.


def render_table(table: Table) -> str:
    """Render one table for the SQL Agent prompt."""

    lines = [f"TABLE {table.name} — {table.description}"]
    lines += [
        f"  {column.name}: {column.description}"
        for column in table.columns
    ]
    return "\n".join(lines)


def render_tables(tables: Sequence[Table]) -> str:
    """Render several tables, separated by blank lines."""

    return "\n\n".join(render_table(table) for table in tables)


# ============================================================
# CATALOGUE
# ============================================================

def get_catalogue() -> dict[str, object]:
    """
    Return the safe CRM schema information exposed to the SQL agent.

    Masked monetary fields are intentionally excluded.
    """

    return {
        "tables": {
            DEALS_TABLE.name: {
                "description": DEALS_TABLE.description,
                "columns": [
                    {
                        "name": column.name,
                        "description": column.description,
                    }
                    for column in DEALS_TABLE.columns
                ],
            },
            LEADS_TABLE.name: {
                "description": LEADS_TABLE.description,
                "columns": [
                    {
                        "name": column.name,
                        "description": column.description,
                    }
                    for column in LEADS_TABLE.columns
                ],
            },
            USERS_TABLE.name: {
                "description": USERS_TABLE.description,
                "columns": [
                    {
                        "name": column.name,
                        "description": column.description,
                    }
                    for column in USERS_TABLE.columns
                ],
            },
        },
        "rules": ALL_RULES,
        "relationships": (
            DEALS_RELATIONSHIPS
            + LEADS_RELATIONSHIPS
        ),
        "enums": DEALS_ENUMS,
    }


__all__ = [
    "Column",
    "render_table",  # [claude]
    "render_tables",  # [claude]
    "Table",
    "DEALS_TABLE",
    "LEADS_TABLE",
    "USERS_TABLE",
    "ALL_RULES",
    "DEALS_RULES_ONLY",
    "LEADS_RULES_ONLY",
    "USERS_RULES_ONLY",
    "RULES_BY_TABLE",
    "relationships_for",
    "render_enums",
    "build_rules",
    "GENERIC_RULES",
    "DEALS_RELATIONSHIPS",
    "LEADS_RELATIONSHIPS",
    "DEALS_ENUMS",
    "get_catalogue",
]
