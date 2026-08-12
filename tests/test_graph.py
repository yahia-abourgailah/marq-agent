"""
[claude] Smoke tests for graph construction.

There was no test that imported app.graph at all, so `build_graph()` carried an
ImportError (`from app.db.repositories.deals import deals` — the module exports
the class, not that name) while the suite stayed green. These tests only build
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
    Guards the specific breakage: the builder must construct a DealsRepository,
    not reach for a `deals` attribute on the module.
    """

    import app.graph.builder as builder

    assert hasattr(builder, "DealsRepository")
    assert isinstance(builder.DealsRepository, type)
