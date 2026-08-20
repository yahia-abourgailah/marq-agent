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
from app.graph.agents.workspace import WORKSPACE_AGENT_SYSTEM_PROMPT
from app.sql.agent import build_sql_agent
from app.sql.catalogue import DEALS_TABLE, LEADS_TABLE, USERS_TABLE, Table
from app.sql.executor import SQLExecutor
from app.sql.guard import SQLGuard
from app.tools.analysis import ANALYSIS_TOOLS
from app.tools.charts import CHART_TOOLS
from app.tools.leads import LEADS_TOOLS
from app.tools.sql import build_sql_tool
from app.tools.workspace import WorkspaceContext, build_workspace_tools


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
    # [claude] Charting is in the default set, so every domain can draw
    # what it just measured. It reaches nothing — no database, no files, no
    # network — it only validates numbers the agent already has and hands
    # them to the client to render.
    extra_tools: Sequence[Any] = field(
        default=tuple([*ANALYSIS_TOOLS, *CHART_TOOLS])
    )

    # [claude] Step ceiling for this domain's ReAct loop, or None for the
    # graph default.
    #
    # A ReAct loop spends two steps per tool call, so the default 16 allows
    # about eight. That is generous for "how many contracted deals" and tight
    # for a reconciliation, which needs four calls before it can answer —
    # manifest, read rows, sql_query, compare — leaving no room for a single
    # correction. An end-to-end run through the supervisor hit the ceiling
    # and degraded to "I ran out of steps", which reads to the user as a
    # broken feature rather than a busy one.
    #
    # Per-domain because the ceiling is a property of the work, and the
    # Domain is already where per-domain differences live.
    max_steps: int | None = None

    # [claude] Whether this domain also reaches user-uploaded files.
    #
    # A flag rather than more entries in `extra_tools` because the workspace
    # tools cannot be built at import time: they need a service holding a
    # Qdrant connection and an embedding model, and constructing either one
    # when this module loads is the import-time-singleton mistake that
    # docs/HANDOFF.md already records for `settings` and `app_db`.
    #
    # So the flag says *that* the domain wants them, and
    # build_domain_agent() decides *when* to build them.
    needs_workspace: bool = False

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
    extra_tools=(*ANALYSIS_TOOLS, *CHART_TOOLS, *LEADS_TOOLS),
)


WORKSPACE = Domain(
    name="workspace",
    # [claude] The superset table set, matching DEALS rather than LEADS.
    #
    # Reconciliation is inherently cross-domain: a user's spreadsheet of
    # contracts has to be matched against deals, and a spreadsheet of
    # enquiries against leads, and nothing tells us which one arrived. The
    # sealed-agent rule from the supervisor's docstring applies here with
    # more force than anywhere else — a misroute cannot be rescued mid-answer,
    # and this agent cannot know which tables it needs until it has read the
    # file's columns.
    #
    # This does not widen the CRM surface: it is the same three tables the
    # Deals Agent already has, under the same guard.
    tables=(DEALS_TABLE, LEADS_TABLE, USERS_TABLE),
    system_prompt=WORKSPACE_AGENT_SYSTEM_PROMPT,
    needs_workspace=True,
    # A reconciliation is four tool calls before it can answer, and the
    # default ceiling leaves it no room to correct a single mistake.
    max_steps=28,
)


# Registered domains, by name. The supervisor will route across these.
DOMAINS: dict[str, Domain] = {
    DEALS.name: DEALS,
    LEADS.name: LEADS,
    WORKSPACE.name: WORKSPACE,  # [claude]
}


def build_domain_agent(
    domain: Domain,
    model: Any,
    database: Database | None = None,
    workspace_service: Any = None,
):
    """
    Build one domain agent with a data surface matching its Domain.

    The same table set feeds the SQL Agent's prompt and the SQLGuard
    allowlist, so the agent is never shown a table the guard would reject,
    and never permitted one the prompt did not describe.

    [claude] `workspace_service` is injected for domains with
    needs_workspace set. Passing None builds the default service on first
    use; the tests pass a service backed by a temporary directory and a
    fake embedder, which is what keeps the workspace path hermetic.
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

    tools = [sql_tool, *domain.extra_tools]

    if domain.needs_workspace:
        if workspace_service is None:
            from app.workspace.service import get_workspace_service

            workspace_service = get_workspace_service()

        tools.extend(build_workspace_tools(workspace_service))

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=domain.system_prompt,
        # [claude] Declared for every domain, not just the workspace one, so
        # make_domain_node has a single invocation path. Agents whose tools
        # never read the context are unaffected by its presence.
        context_schema=WorkspaceContext,
    )


__all__ = [
    "DEALS",
    "LEADS",
    "WORKSPACE",  # [claude]
    "DOMAINS",
    "Domain",
    "build_domain_agent",
]
