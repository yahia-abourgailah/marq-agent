from __future__ import annotations

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
        Column("batch_date", "Batch date."),
        Column("batch_number", "Batch number."),

        # Ownership
        Column("agent_id", "Current deal agent / owner user ID."),
        Column("creator_id", "User ID that originally created the deal."),
        Column("team_leader_id", "Team leader user ID."),
        Column("franchise_id", "Franchise / branch ID."),
        Column("owner_id", "Deal owner user ID."),

        # Lead / opportunity / attribution
        Column(
            "lead_id",
            "Related lead ID. Joins directly to leads.id.",
        ),
        Column(
            "opportunity_id",
            (
                "Related opportunity ID. Usually NULL. "
                "Do not use this as the primary path from deals to leads."
            ),
        ),
        Column(
            "deal_source_id",
            "Deal source ID. Joins to lead_sources.id.",
        ),
        Column(
            "deal_lead_source_id",
            "Contains a leads.id despite its misleading name.",
        ),
        Column("last_lead_source_id", "Last lead source ID."),
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
            "Attribution resolution flow: 1, 2, or 3.",
        ),
        Column(
            "lead_occurrence_count",
            "Number of lead occurrences.",
        ),

        # Inventory
        Column("unit_number", "Unit number."),
        Column("project_id", "Project ID."),
        Column("developer_id", "Developer ID."),
        Column("location_id", "Location ID."),
        Column("unit_type_id", "Unit type ID."),
        Column("finishing_type_id", "Finishing type ID."),
        Column("area", "Unit area stored by the CRM."),
        Column(
            "selling_type",
            "Selling type. Allowed values are primary or resale.",
        ),
        Column(
            "delivery_date",
            "Delivery year stored as a numeric value, not a timestamp.",
        ),
        Column("payment_plan", "Payment plan description."),

        # Approval / collection
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
            "Whether the deal is classified as commercial.",
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
            "Business/reporting date used by CRM scopes and reports.",
        ),
        Column(
            "expected_closing_date",
            "Expected closing date.",
        ),

        # Client information
        Column("client_name", "Client name."),
        Column("client_name_ar", "Client name in Arabic."),
        Column("nationality", "Client nationality."),
        Column("social_status", "Client social status."),
        Column("job_title", "Client job title."),
        Column("country", "Client country."),
        Column("city", "Client city."),

        # Other
        Column(
            "cumulative_sales_at_deal",
            "Cumulative sales at the time of the deal.",
        ),
        Column(
            "has_retroactive_adjustments",
            "Whether the deal has retroactive adjustments.",
        ),
        Column("last_comment", "Latest deal comment."),
    ),
)


DEALS_RULES = """
Deals SQL rules:

1. TABLE AVAILABILITY
   - The SQL agent currently has access to the `deals` table only.
   - Do not invent or query tables that are not explicitly provided in
     the catalogue.
   - Do not use users, leads, projects, developers, locations, unit_types,
     or other related tables unless those tables are explicitly added to
     the SQL catalogue.
   - Foreign-key relationships listed below are informational only and
     must not be used to generate joins unless the referenced table is
     also available in the catalogue.

2. SOFT DELETION
   - Live CRM records have:
       deleted_at IS NULL
   - Unless the user explicitly asks about deleted records, always
     exclude deleted records with:
       deleted_at IS NULL

3. DEAL STATUS
   The `status` column has only these known values:
   - cancelled
   - eoi
   - contracted
   - reservation

   Never invent a status value such as:
   - active
   - open
   - negotiation
   - won

4. ACTIVE DEALS
   The CRM does not have an `active` status.

   For this catalogue, "active deal" means a non-cancelled,
   non-deleted deal.

   Therefore use:

       status IN ('eoi', 'reservation', 'contracted')
       AND deleted_at IS NULL

   This definition is a business interpretation for the SQL agent.
   If the real CRM provides a different definition of "active",
   this rule must be updated.

5. CANCELLED DEALS
   When the user explicitly asks for cancelled deals, use:

       status = 'cancelled'

   Deleted records should still normally be excluded unless the user
   explicitly asks for deleted records.

6. EOI DEALS
   "EOI" or "expression of interest" means:

       status = 'eoi'

7. RESERVATION DEALS
   "Reservation" means:

       status = 'reservation'

8. CONTRACTED DEALS
   "Contracted" means:

       status = 'contracted'

9. COUNTING DEALS
   When the user asks how many deals exist, use:

       COUNT(*)

   and normally include:

       deleted_at IS NULL

10. OWNER
    The deals table contains:
       owner_id
       agent_id
       creator_id
       team_leader_id

    There is no owner_name column in the deals table.

    Do not invent owner_name.

    If the user provides a numeric owner ID, filter directly using
    owner_id.

    If the user provides only a person's name, the SQL agent must not
    invent a users table or users.name column. The request cannot be
    reliably resolved from the deals table alone.

11. PROJECT
    The deals table contains project_id only.

    There is no project_name column.

    Do not invent project_name.

    If the user provides a project ID, filter using project_id.

12. UNIT
    Unit information available directly on deals includes:
       unit_number
       unit_type_id
       finishing_type_id
       area
       selling_type
       delivery_date
       payment_plan

13. SELLING TYPE
    `selling_type` has these known values:
       primary
       resale

14. APPROVAL FIELDS
    The approval fields use:
       pending
       accepted
       rejected

15. COLLECTION STATUS
    `collection_amount_status` has:
       pending
       half_collected
       fully_collected

16. COMMERCIAL
    `is_commercial` indicates whether the deal is classified as
    commercial.

17. DATE FIELDS
    Use the correct column names exactly:

       created_at
       updated_at
       reservation_date
       contract_date
       contract_date_added_at
       cancellation_date
       transaction_date
       expected_closing_date
       collected_at

    Do not invent alternative names such as:
       expected_close_date
       expected_closing_at

18. CLOSING SOON
    When the user asks which deals are closing soon, use:

       expected_closing_date

    and compare it against CURRENT_DATE or an appropriate future
    date range.

    Do not use a nonexistent `expected_close_date` column.

19. DEAL VALUE
    The current deals catalogue contains no deal value / price column.

    Do not invent:
       value
       price
       amount
       currency

    Questions requiring deal monetary value cannot currently be
    answered from the deals table alone.

20. CLIENT INFORMATION
    Client information available directly on deals includes:

       client_name
       client_name_ar
       nationality
       social_status
       job_title
       country
       city

21. SECURITY
    Generate read-only SELECT queries only.

    Never generate:
       INSERT
       UPDATE
       DELETE
       DROP
       ALTER
       CREATE
       TRUNCATE

    Permission and row-level access restrictions are enforced outside
    the SQL agent by the database/security layer.
"""


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
)


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


def get_deals_catalogue() -> dict[str, object]:
    """Return the safe Deals schema information for the SQL agent."""

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
            }
        },
        "rules": DEALS_RULES,
        "relationships": DEALS_RELATIONSHIPS,
        "enums": DEALS_ENUMS,
    }


__all__ = [
    "Column",
    "Table",
    "DEALS_TABLE",
    "DEALS_RULES",
    "DEALS_RELATIONSHIPS",
    "DEALS_ENUMS",
    "get_deals_catalogue",
]