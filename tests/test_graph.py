"""
[claude] Smoke tests for graph construction.

There was no test that imported app.graph at all, so `build_graph()` carried
an ImportError — it reached for a `deals` attribute on the repository module,
which exports a class — while the suite stayed green. These tests only build
and inspect the graph; they make no model or database calls.
"""

from __future__ import annotations


def test_graph_module_imports():
    """app.graph.graph builds a graph at import time — it must not raise."""

    from app.graph.graph import graph

    assert graph is not None


def test_build_graph_compiles():
    from app.graph.builder import build_graph

    compiled = build_graph()

    assert compiled is not None


def test_graph_exposes_the_deals_agent_node():
    from app.graph.builder import build_graph

    compiled = build_graph()

    assert "deals_agent" in compiled.get_graph().nodes


def test_build_graph_wires_a_real_repository():
    """
    Guards the original breakage: the wiring must construct the repository
    class, not reach for a `deals` attribute on its module.
    """

    from app.db.repositories.sql import SQLRepository
    from app.graph.agents.domain import DEALS, build_domain_agent

    assert isinstance(SQLRepository, type)
    assert callable(build_domain_agent)
    assert DEALS.tables


def test_guard_and_prompt_agree_on_the_same_tables():
    """
    [claude] The invariant that makes a second domain agent safe.

    SQLGuard used to resolve its allowlist from the global catalogue, so
    every agent shared one query surface. A domain's guard must permit
    exactly the tables its prompt describes — no more.
    """

    from app.graph.agents.domain import DOMAINS
    from app.sql.guard import SQLGuard

    for name, domain in DOMAINS.items():
        guard = SQLGuard(tables=frozenset(domain.table_names))

        assert guard.allowed_tables() == set(domain.table_names), name


def test_narrowing_a_domain_narrows_the_guard():
    """A domain restricted to one table must not permit the others."""

    from app.sql.catalogue import DEALS_TABLE
    from app.sql.guard import SQLGuard, SQLGuardError

    guard = SQLGuard(tables=frozenset({DEALS_TABLE.name}))

    guard.validate("SELECT id FROM deals")

    for blocked in ("SELECT id FROM leads", "SELECT id FROM users"):
        try:
            guard.validate(blocked)
        except SQLGuardError:
            continue
        raise AssertionError(f"guard should have rejected: {blocked}")
