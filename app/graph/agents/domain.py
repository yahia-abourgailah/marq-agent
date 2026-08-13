"""
[claude] Domain definitions and the generic agent builder.

Why this exists
---------------
Wiring a domain agent means keeping four things in agreement:

    the tables its SQL Agent is shown
    the tables its SQLGuard permits
    the rules and relationships in its prompt
    its own system prompt and tools

Before this, those were spread across builder.py, agent.py and guard.py, and
three of them were global — SQLGuard called the catalogue itself, so every
guard in the process allowed the same tables no matter which agent held it.
That is workable with one agent and wrong with several: a Leads Agent and a
Deals Agent should not share a query surface.

A Domain states the four together, and build_domain_agent() derives the whole
stack from it, so the guard can never permit a table the prompt never
described.

Adding the Leads Agent
----------------------
Declare a Domain and register it:

    LEADS = Domain(
        name="leads",
        tables=(LEADS_TABLE, USERS_TABLE),
        system_prompt=LEADS_AGENT_SYSTEM_PROMPT,
    )

No changes to the guard, the SQL agent, the repository or the tool.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent

from app.db.connection import Database, app_db
from app.db.repositories.sql import SQLRepository
from app.graph.agents.deals import DEALS_AGENT_SYSTEM_PROMPT
from app.graph.agents.leads import LEADS_AGENT_SYSTEM_PROMPT
from app.sql.agent import build_sql_agent
from app.sql.catalogue import DEALS_TABLE, LEADS_TABLE, USERS_TABLE, Table
from app.sql.executor import SQLExecutor
from app.sql.guard import SQLGuard
from app.tools.analysis import ANALYSIS_TOOLS
from app.tools.leads import LEADS_TOOLS
from app.tools.sql import build_sql_tool


@dataclass(frozen=True)
class Domain:
    """One conversational agent and the data surface it is allowed."""

    name: str

    # The tables this domain may query. Drives the SQL Agent's schema block,
    # its rules and relationships, and the SQLGuard allowlist — one source,
    # so they cannot disagree.
    tables: tuple[Table, ...]

    # The domain agent's own system prompt.
    system_prompt: str

    # Tools beyond sql_query. The analysis tools are arithmetic helpers with
    # no domain knowledge, so every domain gets them.
    extra_tools: Sequence[Any] = field(default=tuple(ANALYSIS_TOOLS))

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(table.name for table in self.tables)


DEALS = Domain(
    name="deals",
    # Deals questions routinely resolve owner names through users and reach
    # the originating lead through deals.lead_id, so all three are in scope.
    tables=(DEALS_TABLE, LEADS_TABLE, USERS_TABLE),
    system_prompt=DEALS_AGENT_SYSTEM_PROMPT,
)


LEADS = Domain(
    name="leads",
    # [claude] Deliberately narrower than DEALS: leads and users only.
    #
    # Conversion is recorded on the lead itself (converted_at,
    # converted_to_opportunity_id), so the funnel questions this agent exists
    # for are answerable without reaching into deals. Keeping deals out is
    # what makes the guard scoping real rather than decorative — this agent's
    # guard rejects `SELECT ... FROM deals`.
    #
    # A question that genuinely spans both — "which lead sources produce the
    # most contracted deals" — is a routing problem, and belongs to the
    # supervisor rather than to either agent widening its own surface.
    tables=(LEADS_TABLE, USERS_TABLE),
    system_prompt=LEADS_AGENT_SYSTEM_PROMPT,
    extra_tools=(*ANALYSIS_TOOLS, *LEADS_TOOLS),
)


# Registered domains, by name. The supervisor will route across these.
DOMAINS: dict[str, Domain] = {
    DEALS.name: DEALS,
    LEADS.name: LEADS,
}


def build_domain_agent(
    domain: Domain,
    model: Any,
    database: Database | None = None,
):
    """
    Build one domain agent with a data surface matching its Domain.

    The same table set feeds the SQL Agent's prompt and the SQLGuard
    allowlist, so the agent is never shown a table the guard would reject,
    and never permitted one the prompt did not describe.
    """

    if database is None:
        database = app_db

    sql_agent = build_sql_agent(model, tables=domain.tables)

    repository = SQLRepository(
        executor=SQLExecutor(database=database),
        guard=SQLGuard(tables=frozenset(domain.table_names)),
    )

    sql_tool = build_sql_tool(
        sql_agent=sql_agent,
        repository=repository,
    )

    return create_agent(
        model=model,
        tools=[sql_tool, *domain.extra_tools],
        system_prompt=domain.system_prompt,
    )


__all__ = [
    "DEALS",
    "LEADS",
    "DOMAINS",
    "Domain",
    "build_domain_agent",
]
