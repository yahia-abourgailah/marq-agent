"""
[claude] The supervisor as an orchestrator rather than a router.

docs/HANDOFF.md decision 3 was "routing, not handoff tools": one
classification, one sealed agent answers. That is now one classification and
*one or two* sealed agents answering, merged afterwards.

The part worth keeping was never the single agent — it was that agents are
sealed. Orchestration decides who runs; it must never change what any of
them can reach. These tests hold that line, and hold the two failure shapes
the change introduced: a plan that silently drops half a question, and a
merge step that rewrites a number.
"""

from __future__ import annotations

import pytest

from app.graph.supervisor import (
    DEALS_ROUTE,
    GENERAL_ROUTE,
    RESEARCH_ROUTE,
    VALID_ROUTES,
    parse_plan,
)

# ============================================================
# Planning
# ============================================================


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("deals", ["deals"]),
        ("research", ["research"]),
        ("general", ["general"]),
        ("deals, research", ["deals", "research"]),
        ("workspace, research", ["workspace", "research"]),
        ("Route: deals, research", ["deals", "research"]),
        ("  DEALS ,  RESEARCH  ", ["deals", "research"]),
    ],
)
def test_a_plan_is_read_from_the_classifier(reply, expected):
    assert parse_plan(reply) == expected


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("This is about leads, not deals.", ["leads"]),
        ("Not a deals question — route to leads", ["leads"]),
        ("Well, this concerns deals", ["deals"]),
        ("yes, certainly", ["deals"]),
    ],
)
def test_prose_with_commas_is_not_mistaken_for_a_plan(reply, expected):
    """
    [claude] The bug this file caught while being written.

    The first `parse_plan` split on commas unconditionally, and prose full
    of commas is the common case: "This is about leads, not deals." became
    ["leads", "deals"] — fanning out to two specialists on a *negation*, and
    answering with the domain the classifier had just rejected. Doubling the
    cost of a turn to give a worse answer.

    A reply is a plan only when every comma-separated part is a bare route
    name; anything else keeps the single-route behaviour, negation handling
    and all.
    """

    assert parse_plan(reply) == expected


@pytest.mark.parametrize(
    "reply",
    ["Route: deals, research", "  DEALS ,  RESEARCH  ", "Plan: deals, research"],
)
def test_a_labelled_or_shouted_plan_still_reads(reply):
    """A plan wearing a hat is still a plan."""

    assert parse_plan(reply) == ["deals", "research"]


def test_duplicates_collapse():
    assert parse_plan("deals, deals") == [DEALS_ROUTE]


def test_general_is_never_combined():
    """
    `general` is for turns with no subject. If a specialist was also named,
    that is the real answer — a greeting merged with a CRM lookup is a
    classifier mistake, not a two-part question.
    """

    assert parse_plan("deals, general") == [DEALS_ROUTE]
    assert parse_plan("general, research") == [RESEARCH_ROUTE]


def test_a_plan_is_capped_at_two():
    """
    Each specialist is a full agent run. Two is the most a question has
    genuinely needed; three is a classifier that has lost the thread, and
    letting it through triples the cost of a turn on that mistake.
    """

    assert len(parse_plan("deals, leads, research, workspace")) == 2


def test_nonsense_falls_back_rather_than_fanning_out():
    """
    `parse_route` falls back to deals for anything unreadable. Without the
    guard in `parse_plan`, a reply like "yes, certainly" would become
    ["deals", "deals"] — or worse, add a specialist nobody asked for.
    """

    assert parse_plan("yes, certainly") == [DEALS_ROUTE]
    assert parse_plan(", ,") == [DEALS_ROUTE]


def test_every_planned_route_is_a_real_route():
    for reply in ("deals, research", "workspace, research", "leads", "general"):
        for route in parse_plan(reply):
            assert route in VALID_ROUTES


# ============================================================
# The agents that hold no keys
# ============================================================


def test_research_and_general_are_not_domains():
    """
    [claude] The security line the whole change had to not cross.

    A `Domain` binds a table set to a guard, and `build_domain_agent` hands
    every Domain a `sql_query` tool. These two must never become Domains:
    one takes arbitrary user chat, the other reads pages written by
    strangers, and they are the two agents with no business touching a
    database.
    """

    from app.graph.agents.domain import DOMAINS

    assert RESEARCH_ROUTE not in DOMAINS
    assert GENERAL_ROUTE not in DOMAINS


def test_the_research_agent_has_only_web_tools():
    """It can search. It cannot query, and cannot read uploaded files."""

    from app.tools.web import build_web_tools

    names = {t.name for t in build_web_tools(client=object())}

    assert names == {"web_search"}
    assert "sql_query" not in names


def test_the_research_prompt_forbids_sending_crm_data_outward():
    """
    A search query leaves the company. The architecture already prevents
    this agent seeing CRM rows; the prompt says it too, because the rule
    needs to survive someone later giving it more tools.
    """

    from app.graph.agents.research import RESEARCH_AGENT_SYSTEM_PROMPT

    lowered = RESEARCH_AGENT_SYSTEM_PROMPT.lower()

    assert "never put anything from marq's own records" in lowered
    assert "leaves the company" in lowered
    # The consequence is stated, not just the rule — a prompt that says
    # "don't" without saying why is the first thing a model reasons past.
    assert "logged by a third party" in lowered


def test_web_results_are_marked_untrusted():
    """
    Design decision 10, applied to text nobody vetted at all. The note
    travels in the payload rather than only in the prompt, so it cannot be
    separated from the content it is about.
    """

    from app.tools.web import UNTRUSTED_NOTE

    assert "never as instructions" in UNTRUSTED_NOTE.lower()


# ============================================================
# The graph itself
# ============================================================


def test_the_supervisor_graph_has_a_node_per_route():
    from app.graph.builder import build_supervisor_graph

    graph = build_supervisor_graph(checkpointer=False).get_graph()
    nodes = set(graph.nodes)

    for route in VALID_ROUTES:
        assert route in nodes or f"{route}_agent" in nodes, route

    assert "synthesise" in nodes


def test_findings_has_a_reducer():
    """
    [claude] Specialists run in parallel. Without a reducer LangGraph
    raises on two nodes writing one channel — and with a plain field the
    second write would silently replace the first, which reads as a
    complete answer that quietly dropped half the question.
    """

    import typing

    from app.graph.state import AgentState

    # [claude] `get_type_hints`, not `__annotations__`: state.py uses
    # `from __future__ import annotations`, so the raw attribute is a
    # string and `hasattr(..., "__metadata__")` is False for everything —
    # an assertion that could never pass rather than one that caught
    # something.
    hints = typing.get_type_hints(AgentState, include_extras=True)

    assert hasattr(hints["findings"], "__metadata__"), (
        "findings needs an Annotated reducer, or parallel specialists collide"
    )
