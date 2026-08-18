"""
[claude] Supervisor routing tests.

Route parsing and the graph's wiring are hermetic — no model, no database.
Whether the classifier picks the *right* route for a real question is a
behavioural question, and lives in evals/routing_cases.py.
"""

from __future__ import annotations

import pytest

from app.graph.supervisor import (
    DEALS_ROUTE,
    FALLBACK_ROUTE,
    LEADS_ROUTE,
    OUT_OF_SCOPE,
    VALID_ROUTES,
    _conversation,
    parse_route,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("deals", DEALS_ROUTE),
        ("leads", LEADS_ROUTE),
        ("out_of_scope", OUT_OF_SCOPE),
        # A small model asked for one word rarely gives exactly one word.
        ("Deals.", DEALS_ROUTE),
        ("  LEADS\n", LEADS_ROUTE),
        ("Route: deals", DEALS_ROUTE),
        ("The answer is leads", LEADS_ROUTE),
        ("out_of_scope — not a CRM question", OUT_OF_SCOPE),
    ],
)
def test_parse_route_is_forgiving(raw, expected):
    assert parse_route(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "banana", "I don't know", "42"])
def test_unparseable_output_falls_back_to_the_superset_domain(raw):
    """
    DEALS sees deals, leads and users; LEADS sees only leads and users. So a
    misroute to DEALS can still be answered, and a misroute to LEADS cannot.
    The fallback must therefore be DEALS.
    """

    assert parse_route(raw) == FALLBACK_ROUTE == DEALS_ROUTE


def test_every_route_is_valid():
    for raw in ("deals", "leads", "out_of_scope", "nonsense"):
        assert parse_route(raw) in VALID_ROUTES


# ============================================================
# Conversation trimming
# ============================================================


class FakeMessage:
    def __init__(self, type_, content):
        self.type = type_
        self.content = content


def test_conversation_keeps_only_human_and_assistant_text():
    """
    Tool calls and tool results are an implementation detail of the previous
    turn. Feeding them to the classifier would swamp the actual question.
    """

    messages = [
        FakeMessage("human", "How many active deals?"),
        FakeMessage("ai", ""),  # the tool-calling turn carries no text
        FakeMessage("tool", '{"success": true, "data": [{"n": 245}]}'),
        FakeMessage("ai", "There are 245 active deals."),
        FakeMessage("human", "and how many of those closed?"),
    ]

    assert _conversation(messages) == [
        {"role": "user", "content": "How many active deals?"},
        {"role": "assistant", "content": "There are 245 active deals."},
        {"role": "user", "content": "and how many of those closed?"},
    ]


def test_conversation_is_bounded():
    messages = [FakeMessage("human", f"q{i}") for i in range(50)]

    assert len(_conversation(messages, limit=6)) == 6


# ============================================================
# Graph wiring
# ============================================================


def test_supervisor_graph_has_a_node_per_domain():
    from app.graph.agents.domain import DOMAINS
    from app.graph.builder import build_supervisor_graph

    nodes = set(build_supervisor_graph().get_graph().nodes)

    assert "supervisor" in nodes
    assert OUT_OF_SCOPE in nodes
    for name in DOMAINS:
        assert f"{name}_agent" in nodes, name


def test_single_domain_graphs_still_build():
    """The supervisor is additive; the per-domain graphs keep working."""

    from app.graph.agents.domain import DEALS, LEADS
    from app.graph.builder import build_graph

    assert "deals_agent" in build_graph(DEALS).get_graph().nodes
    assert "leads_agent" in build_graph(LEADS).get_graph().nodes


def test_studio_entry_points_build():
    from app.graph.builder import (
        build_leads_studio_graph,
        build_studio_graph,
        build_supervisor_studio_graph,
    )

    for build in (
        build_studio_graph,
        build_leads_studio_graph,
        build_supervisor_studio_graph,
    ):
        assert build() is not None


# ============================================================
# [claude] parse_route on prose replies.
#
# It used to substring-match in VALID_ROUTES order, so any reply that was not
# a bare word resolved to whichever route name appeared earliest in the tuple
# rather than the one the classifier chose. out_of_scope could never win
# against a reply naming another route, so an off-topic question ran a full
# CRM agent instead of declining in one line.
#
# The 37 routing eval cases all passed because they exercise the one-word
# path — which the model does take almost always, and which the handoff notes
# is least reliable exactly when vLLM's batching makes decoding
# non-reproducible.
# ============================================================


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        # Position cannot settle these two: the first wants the last mention,
        # the second wants the first. The negation is what distinguishes them.
        ("Not a deals question — route to leads", "leads"),
        ("This is about leads, not deals.", "leads"),
        ("out_of_scope: I can't help with deals data", "out_of_scope"),
        ("no workspace file; this is a leads question", "leads"),
        ("This is a workspace question, not deals", "workspace"),
        ("I'd say leads rather than deals", "leads"),
        ("not workspace, not leads — deals", "deals"),
        ("This is chit-chat, so out_of_scope", "out_of_scope"),
    ],
)
def test_prose_replies_resolve_to_the_route_the_classifier_meant(
    reply, expected
):
    assert parse_route(reply) == expected


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("deals", "deals"),
        ("leads", "leads"),
        ("workspace", "workspace"),
        ("out_of_scope", "out_of_scope"),
        ("  DEALS.  ", "deals"),
        ("Deals", "deals"),
        ("Route: leads", "leads"),
        ("**workspace**", "workspace"),
        ("leads\n", "leads"),
    ],
)
def test_the_one_word_path_still_works(reply, expected):
    """The path the classifier actually takes, and the eval cases exercise."""

    assert parse_route(reply) == expected


@pytest.mark.parametrize("reply", ["", "   ", "???", "I have no idea", "🙂"])
def test_unrecognisable_replies_fall_back_to_the_superset_domain(reply):
    assert parse_route(reply) == FALLBACK_ROUTE


def test_out_of_scope_is_reachable_from_prose():
    """
    The regression that mattered most: out_of_scope could never win, so an
    off-topic question cost a database round-trip to discover what a one-line
    decline already knew.
    """

    for reply in (
        "out_of_scope",
        "out_of_scope — not a deals question",
        "I think this is out_of_scope",
    ):
        assert parse_route(reply) == OUT_OF_SCOPE
