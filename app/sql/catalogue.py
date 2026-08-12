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
        Column("agent_id", "Current deal owner / agent user ID."),
        Column("creator_id", "User ID that originally created the deal."),
        Column("team_leader_id", "Team leader user ID."),
        Column("franchise_id", "Franchise / branch ID."),
        Column("owner_id", "Deal owner user ID."),

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

DEALS_RULES = """
Deals SQL rules:

1. SOURCE OF TRUTH
   The CRM schema represented by this catalogue is based on the
   verified MyTAI CRM database schema.

   Never invent tables, columns, enum values, or relationships.

2. AVAILABLE TABLES
   The SQL agent may query only tables explicitly included in the
   catalogue.

   Currently available:
       deals
       leads
       users

   Relationships to projects, developers, locations, unit_types,
   finishing_types, opportunities, lead_sources, and other tables are
   documented for reference but those tables are NOT currently available
   for SQL generation unless they are explicitly added to the catalogue.

3. SOFT DELETES
   `deals` and `leads` use soft deletion.

   Unless the user explicitly asks for deleted records, always include:

       deleted_at IS NULL

   for the relevant table.

4. DEAL STATUS
   The `deals.status` column has exactly these known values:

       cancelled
       eoi
       contracted
       reservation

   Never invent statuses such as:

       active
       open
       negotiation
       won
       closed

5. ACTIVE DEALS
   There is no `active` status.

   For deal queries, "active deals" means non-cancelled and non-deleted:

       status IN ('eoi', 'reservation', 'contracted')
       AND deleted_at IS NULL

6. CLOSED / WON DEALS
   A closed/won deal is:

       status = 'contracted'

7. CANCELLED DEALS
   Cancelled deals are:

       status = 'cancelled'

   A cancelled deal may still have deleted_at IS NULL.

   Therefore do not use soft deletion alone to determine whether a
   deal is cancelled.

8. EOI
   EOI / Expression of Interest means:

       status = 'eoi'

9. RESERVATION
   Reservation means:

       status = 'reservation'

10. CONTRACTED
    Contracted means:

        status = 'contracted'

11. NEGOTIATION
    The CRM schema does not define a `negotiation` status.

    Do not invent a mapping for "negotiation".

    Do not use:

        status NOT IN (...)

    to guess what negotiation means.

    If a question requires a definition of negotiation that is not
    provided by the catalogue, the request cannot be reliably resolved
    from the available deal status definitions.

12. COUNTING DEALS
    When asked how many deals exist, normally use:

        COUNT(*)

    together with:

        deleted_at IS NULL

    unless the user explicitly asks for deleted records.

13. DEAL OWNERS
    The deals table contains:

        owner_id
        agent_id
        creator_id
        team_leader_id

    These IDs can be resolved using the available `users` table.

    For a user-name query, a valid pattern is:

        JOIN users u ON deals.owner_id = u.id

    followed by a filter on:

        u.name

    Do not invent user columns other than those in the catalogue.

14. USER REPORTING TREE
    `users.parent_id` represents the reporting hierarchy.

    Do not automatically apply hierarchy filtering in generated SQL.

    Deal visibility is enforced by the database/security layer.

15. DEAL VISIBILITY
    The database/security layer is responsible for enforcing the
    user's allowed deal rows.

    The SQL agent must not attempt to bypass or weaken database
    permissions or row-level security.

    Do not add unrestricted access logic merely because the user asks
    for "all deals".

16. DEAL TO LEAD RELATIONSHIP
    The primary relationship between deals and leads is:

        deals.lead_id = leads.id

    Do not use opportunities as the primary path from deals to leads.

    A lead can have multiple deals.

    Do not assume one lead maps to exactly one deal.

17. DEAL LEAD SOURCE FIELD
    `deals.deal_lead_source_id` is misleadingly named.

    It contains a `leads.id`, not a `lead_sources.id`.

    Do not join it to lead_sources.

18. DEAL DATES
    Use the exact available date columns:

        created_at
        updated_at
        reservation_date
        contract_date
        contract_date_added_at
        cancellation_date
        transaction_date
        expected_closing_date
        collected_at
        batch_date

    Do not invent alternative names.

19. BUSINESS DATE
    `transaction_date` is the business/reporting date used by CRM
    scopes and reports.

    When the user asks for CRM reporting-period performance, prefer
    transaction_date when the question is clearly about business
    reporting rather than record creation.

20. CLOSING SOON
    When asked which deals are closing soon, use:

        expected_closing_date

    Do not use a nonexistent expected_close_date column.

    "Soon" means upcoming. Exclude dates that have already passed,
    otherwise the earliest rows returned are deals that closed long ago:

        WHERE expected_closing_date >= CURRENT_DATE
        ORDER BY expected_closing_date ASC

    Only drop the CURRENT_DATE filter when the user explicitly asks about
    past or overdue closing dates.

21. DELIVERY DATE
    `delivery_date` is NOT a timestamp.

    It is a double precision value representing a year number.

    Do not use timestamp operations directly on delivery_date.

22. UNIT AREA
    `area` is stored as varchar.

    Do not assume it is numeric without an explicit cast.

23. SELLING TYPE
    `selling_type` has:

        primary
        resale

24. APPROVAL ENUMS
    The following fields have:

        pending
        accepted
        rejected

    Fields:
        franchise_owner_approval
        sales_operation_approval
        collection_approval

25. COLLECTION STATUS
    `collection_amount_status` has:

        pending
        half_collected
        fully_collected

26. COMMERCIAL DEALS
    `is_commercial = true` means the deal is classified as commercial.

    `is_commercial = false` means it is not commercial.

27. CLIENT INFORMATION
    Client information is available on deals through:

        client_name
        client_name_ar
        national_id
        national_id_address
        birth_date
        nationality
        social_status
        job_title
        working_email
        living_address
        correspondence_address
        country
        city

    Treat PII according to the database/security permissions.

28. LEAD INFORMATION
    Lead information can be accessed through:

        deals.lead_id = leads.id

    Do not invent lead columns.

29. LEAD DEDUPLICATION
    Leads can be duplicated.

    When the user explicitly asks for unique demand/leads, consider:

        merged_into_id IS NULL

    according to the CRM deduplication rules.

    Do not automatically deduplicate normal deal counts.

30. MASKED MONEY / RESTRICTED COLUMNS

    The underlying CRM database contains monetary fields that are
    restricted from SQL access for this agent.

    The agent may be aware that these fields exist conceptually, but
    they are NOT usable SQL columns.

    The following identifiers are STRICTLY FORBIDDEN in generated SQL:

        unit_price
        reservation_price
        contract_price
        collection_price
        down_payment
        total_retroactive_commission

    NEVER reference these identifiers in any generated SQL.

    This includes:

        SELECT
        WHERE
        GROUP BY
        ORDER BY
        HAVING
        JOIN
        subqueries
        CTEs
        aggregate functions
        expressions
        aliases

    For example, NEVER generate:

        SELECT SUM(contract_price) FROM deals;

        SELECT AVG(unit_price) FROM deals;

        SELECT reservation_price FROM deals;

    If the user asks for information that requires one of these
    restricted fields, do NOT attempt to answer by generating SQL.

    Do NOT substitute:

        value
        price
        amount
        currency

    unless that column is explicitly present and permitted by the
    catalogue.

    Instead, return a concise statement that the requested information
    cannot be retrieved through the available CRM data access.

    IMPORTANT:

    Knowing that a restricted column exists does NOT grant permission
    to query it.
31. MASKED LEAD MONEY
    The following lead columns are masked and MUST NOT be generated:

        budget_amount
        cost_per_lead
        ad_spend_amount

32. MASKED / EXCLUDED FREE TEXT
    `leads.last_activity_feedback` is intentionally excluded from this
    catalogue because the source security/masking specification marks it
    unavailable to the agent.

33. SECURITY
    The SQL agent only generates read-only SELECT queries.

    Never generate:

        INSERT
        UPDATE
        DELETE
        DROP
        ALTER
        CREATE
        TRUNCATE
        MERGE
        GRANT
        REVOKE

    Database permissions and row-level security are enforced outside
    the SQL agent.

34. NO SECURITY BYPASS
    Never generate SQL intended to bypass:

        PostgreSQL permissions
        row-level security
        application visibility rules
        masked-column restrictions

35. PRECISE PROJECTIONS
    Prefer selecting only the columns required to answer the question.

    Do not use SELECT * unless the user explicitly requests the complete
    record.

    When returning individual records rather than an aggregate, always
    include an identifying column so the rows can be referred to:

        deals   -> id, and unit_number where relevant
        leads   -> id, name
        users   -> id, name

    A result of bare values with no identifier cannot be reported back
    to the user usefully.

36. AGGREGATIONS
    Use PostgreSQL aggregation functions where appropriate:

        COUNT
        AVG
        MIN
        MAX
        SUM

    Only aggregate over columns actually available in the catalogue.

37. CURRENCY
    The deals schema provided to this agent does NOT expose a currency
    column.

    Do not invent one.

38. NULL HANDLING
    Use PostgreSQL NULL semantics correctly:

        IS NULL
        IS NOT NULL
        COALESCE(...)

    where appropriate.

39. QUERY ACCURACY
    Always use exact table and column names from the catalogue.

    Never create a query merely because it looks plausible.

    If the requested information cannot be obtained from the available
    catalogue, do not fabricate a schema element.

40. READ-ONLY DATABASE ACCESS
    The database role used by the application should independently have
    only the permissions required for read access.

    SQL generation restrictions and database permissions are separate
    security layers.
41. DATABASE-SIDE ANALYSIS

    Perform filtering, sorting, grouping, aggregation, ranking, and
    result selection directly in SQL.

    Do not expect the application model to retrieve a large set of
    rows and perform database-style operations itself.

    Examples:

    "Top 5 deals by area"

        SELECT id, unit_number, area
        FROM deals
        WHERE deleted_at IS NULL
        ORDER BY CAST(area AS numeric) DESC
        LIMIT 5

    "Smallest 10 deals by area"

        SELECT id, unit_number, area
        FROM deals
        WHERE deleted_at IS NULL
        ORDER BY CAST(area AS numeric) ASC
        LIMIT 10

    NOTE: `area` is varchar (rule 22). Ordering it without CAST sorts
    lexicographically, so '97' ranks above '446'. Always cast before
    ordering or comparing it numerically.

    "How many deals are in each status"

        GROUP BY status

    "Which deals are closing soon"

        SELECT id, expected_closing_date
        FROM deals
        WHERE deleted_at IS NULL
          AND expected_closing_date IS NOT NULL
        ORDER BY expected_closing_date ASC

    Use SQL aggregation for exact counts, averages, minimums, maximums,
    and other aggregate questions.

42. DEAL AGING / STALE DEALS

    Deal aging must be calculated from actual date fields available
    in the catalogue.

    Do not invent a `days_in_stage` column.

    If the requested aging concept can be derived from available
    timestamps, calculate it in SQL.

    If the required timestamp or business definition is not available
    in the catalogue, do not invent one.

43. RESULT SIZE

    Use SQL filtering, ordering, aggregation, and LIMIT to minimize
    the number of rows returned.

    Prefer returning the exact rows needed for the user's request
    rather than retrieving a large dataset for model-side processing.
"""


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

def get_deals_catalogue() -> dict[str, object]:
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
        "rules": DEALS_RULES,
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
    "DEALS_RULES",
    "DEALS_RELATIONSHIPS",
    "LEADS_RELATIONSHIPS",
    "DEALS_ENUMS",
    "get_deals_catalogue",
]
