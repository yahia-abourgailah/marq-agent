"""
[claude] Invariants that must hold across the whole project.

These are not unit tests of any one module. They are the cross-cutting
agreements that make the architecture true — and each one, if it drifted,
would fail somewhere far from the change that broke it:

  a domain shown a table its guard rejects
  a supervisor route with no node behind it
  a restricted column readable because someone added it to the catalogue
  a prompt telling an agent to use a tool it does not have

Every one of those is silent. The agent keeps answering; it just answers
wrongly, or refuses things it should be able to do.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import app
from app.graph.agents.domain import DOMAINS, Domain
from app.graph.supervisor import OUT_OF_SCOPE, VALID_ROUTES
from app.sql.catalogue import get_catalogue
from app.sql.guard import SQLGuard, SQLGuardError

# Columns the catalogue must never describe. Masking is enforced by omission
# plus the prompt, so a restricted column appearing in a Table definition
# would silently unmask it — the guard would have no reason to object.
RESTRICTED_COLUMNS = {
    "unit_price",
    "reservation_price",
    "contract_price",
    "collection_price",
    "down_payment",
    "total_retroactive_commission",
    "budget_amount",
    "cost_per_lead",
    "ad_spend_amount",
    # [claude] Added from the live MyTAI schema, which masks these two as
    # well. Both are false positives of that system's money heuristic —
    # `date_ten_percentage` is a date and `last_activity_feedback` is call
    # notes — so they are masked for no good reason and worth revisiting.
    # Until someone decides that, matching the reference implementation is
    # the conservative choice: unmasking a column is a decision, not a
    # cleanup.
    "date_ten_percentage",
    "last_activity_feedback",
}


# ============================================================
# Domains, guards and the catalogue
# ============================================================


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_every_domain_key_matches_its_name(domain: Domain):
    assert DOMAINS[domain.name] is domain


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_a_domains_tables_all_exist_in_the_catalogue(domain: Domain):
    catalogue = set(get_catalogue()["tables"])

    assert set(domain.table_names) <= catalogue


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_a_domains_guard_permits_exactly_its_own_tables(domain: Domain):
    """
    The central agreement: the tables the agent is shown and the tables its
    guard permits come from one source and cannot disagree.
    """

    guard = SQLGuard(tables=frozenset(domain.table_names))

    assert guard.allowed_tables() == {n.lower() for n in domain.table_names}

    for table in domain.table_names:
        guard.validate(f"SELECT 1 FROM {table}")


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_a_domain_cannot_reach_tables_outside_its_own_set(domain: Domain):
    guard = SQLGuard(tables=frozenset(domain.table_names))

    outside = set(get_catalogue()["tables"]) - set(domain.table_names)

    for table in outside:
        with pytest.raises(SQLGuardError):
            guard.validate(f"SELECT 1 FROM {table}")


def test_generic_rules_only_name_tables_every_domain_holds():
    """
    [claude] GENERIC_RULES goes to every agent, so a worked example reading
    `FROM deals` was handed to the Leads Agent — whose guard rejects that
    table outright. It then wrote deals SQL and was refused, which is
    exactly what `leads_agent_cannot_reach_deals` kept catching.

    Only tables present in *every* domain may appear in the shared rules.
    Today that is leads and users; deals belongs in the deals rules.
    """

    from app.sql.catalogue import GENERIC_RULES

    universal = set.intersection(
        *(set(domain.table_names) for domain in DOMAINS.values())
    )

    all_tables = set(get_catalogue()["tables"])

    for table in all_tables - universal:
        assert table not in GENERIC_RULES.lower(), (
            f"GENERIC_RULES names {table!r}, which not every domain can "
            f"query. Universal tables: {sorted(universal)}"
        )


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_a_domains_rules_never_discuss_tables_it_cannot_query(domain: Domain):
    """
    A rule mentioning a table the agent has no access to is at best noise
    and at worst an invitation to write SQL the guard will reject.
    """

    from app.sql.catalogue import build_rules

    rules = build_rules(domain.table_names).lower()
    unreachable = set(get_catalogue()["tables"]) - set(domain.table_names)

    for table in unreachable:
        assert table not in rules, (
            f"the {domain.name} rules mention {table!r}, which its guard "
            "rejects"
        )


def test_no_restricted_column_is_described_in_the_catalogue():
    """
    Masking is by omission. A restricted column added to a Table definition
    would be shown to the SQL agent and permitted by the guard, with nothing
    anywhere to object.
    """

    described = {
        column["name"].lower()
        for table in get_catalogue()["tables"].values()
        for column in table["columns"]
    }

    leaked = described & RESTRICTED_COLUMNS

    assert not leaked, f"restricted columns are in the catalogue: {leaked}"


def test_the_restricted_list_here_matches_the_rules_text():
    """
    This test's own list is a copy, so it has to be checked against the
    catalogue prose it mirrors — otherwise it silently stops covering a
    column someone adds to the rules.
    """

    rules = get_catalogue()["rules"]

    for column in RESTRICTED_COLUMNS:
        assert column in rules, f"{column} is not named in the rules text"


# ============================================================
# Routing
# ============================================================


def test_every_domain_has_a_route_and_every_route_has_a_domain():
    """
    A route with no node falls through to the fallback and answers as the
    wrong specialist. A domain with no route is simply unreachable.
    """

    routes = set(VALID_ROUTES) - {OUT_OF_SCOPE}

    assert routes == set(DOMAINS)


def test_the_supervisor_prompt_names_every_route():
    from app.graph.supervisor import SUPERVISOR_PROMPT

    for route in VALID_ROUTES:
        assert route in SUPERVISOR_PROMPT, f"{route} is not in the prompt"


def test_the_fallback_route_is_a_real_domain():
    from app.graph.supervisor import FALLBACK_ROUTE

    assert FALLBACK_ROUTE in DOMAINS


def test_parse_route_returns_only_valid_routes():
    from app.graph.supervisor import parse_route

    for text in ("", "nonsense", "DEALS.", "Route: leads", "workspace!", "🙂"):
        assert parse_route(text) in VALID_ROUTES


def test_route_matching_is_not_ambiguous():
    """
    parse_route matches substrings in order, so no route name may contain
    another — otherwise the first would swallow the second's answers.
    """

    for route in VALID_ROUTES:
        others = [r for r in VALID_ROUTES if r != route]
        assert not any(other in route for other in others)


# ============================================================
# Prompts and tools
# ============================================================


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_a_prompt_never_promises_a_tool_the_domain_lacks(domain: Domain):
    """
    A prompt naming a tool the agent does not have sends it looking for
    something that is not there, which costs a turn and ends in a refusal
    the user cannot act on.
    """

    available = {"sql_query"}
    available |= {getattr(t, "name", "") for t in domain.extra_tools}

    if domain.needs_workspace:
        available |= {
            "workspace_files",
            "workspace_search",
            "workspace_read_rows",
            "workspace_aggregate",
            "compare_with_crm",
        }

    known_tools = {
        "workspace_files",
        "workspace_search",
        "workspace_read_rows",
        "workspace_aggregate",
        "compare_with_crm",
        "calculate_funnel",
        "calculate_share",
        "calculate_percentage",
        "calculate_percentage_change",
        "calculate_average",
        "calculate_difference",
        "compare_periods",
    }

    promised = {
        tool for tool in known_tools if tool in domain.system_prompt
    }

    missing = promised - available

    assert not missing, f"{domain.name} prompt promises {missing}"


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_every_prompt_forbids_writing_sql_directly(domain: Domain):
    assert "Never write SQL yourself" in domain.system_prompt


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_every_prompt_states_it_is_read_only(domain: Domain):
    assert "read-only" in domain.system_prompt.lower()


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_every_prompt_explains_the_retryable_contract(domain: Domain):
    """
    Every tool reports failure the same way, so every agent has to know how
    to read it. An agent that ignores `retryable` either loops on something
    permanent or gives up on something it could have fixed.
    """

    assert "retryable" in domain.system_prompt


# ============================================================
# Failure payloads
# ============================================================


@pytest.mark.asyncio
async def test_tool_failures_share_one_shape():
    """
    Both tool families report failure with the same keys, so a prompt can
    describe the contract once and every agent can rely on it.
    """

    from app.tools.workspace import build_workspace_tools
    from app.workspace.service import WorkspaceService
    from app.workspace.store import WorkspaceStore

    service = WorkspaceService(store=WorkspaceStore("/nonexistent-root"))
    tools = {t.name: t for t in build_workspace_tools(service)}

    class NoContext:
        context = None

    failure = await tools["workspace_files"].coroutine(runtime=NoContext())

    assert set(failure) >= {"success", "retryable", "reason", "error"}
    assert failure["success"] is False
    assert isinstance(failure["retryable"], bool)
    assert isinstance(failure["error"], str)


def test_every_workspace_tool_hides_the_workspace_id_from_the_model():
    from app.tools.workspace import build_workspace_tools
    from app.workspace.service import WorkspaceService
    from app.workspace.store import WorkspaceStore

    service = WorkspaceService(store=WorkspaceStore("/nonexistent-root"))

    for tool in build_workspace_tools(service):
        assert "workspace_id" not in tool.args
        assert "runtime" not in tool.args


# ============================================================
# Module hygiene
# ============================================================


def _app_modules():
    for info in pkgutil.walk_packages(app.__path__, prefix="app."):
        yield info.name


@pytest.mark.parametrize("name", sorted(_app_modules()))
def test_every_module_imports_cleanly(name):
    """
    Import errors in a rarely-touched module surface at the worst moment —
    when the graph is being built to answer a question.
    """

    importlib.import_module(name)


@pytest.mark.parametrize("name", sorted(_app_modules()))
def test_every_name_in_dunder_all_actually_exists(name):
    module = importlib.import_module(name)

    for exported in getattr(module, "__all__", []):
        assert hasattr(module, exported), f"{name}.{exported} does not exist"


@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
def test_a_domains_step_ceiling_covers_its_longest_workflow(domain: Domain):
    """
    [claude] A ReAct loop spends two steps per tool call. The workspace
    reconciliation needs four calls before it can answer — manifest, read
    rows, sql_query, compare — so 16 steps left it no room to correct a
    single mistake, and an end-to-end run degraded to "I ran out of steps".

    Asserted rather than commented because the ceiling and the workflow live
    in different files, and nothing else would notice them drifting apart.
    """

    from app.graph.builder import MAX_AGENT_STEPS

    ceiling = domain.max_steps or MAX_AGENT_STEPS

    longest_workflow_calls = 4 if domain.needs_workspace else 2
    room_to_retry = 2

    needed = (longest_workflow_calls + room_to_retry) * 2

    assert ceiling >= needed, (
        f"{domain.name} allows {ceiling} steps; its longest workflow needs "
        f"{needed} with room to recover"
    )
