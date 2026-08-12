"""
The compiled application graph, built once at import.
"""

from __future__ import annotations

from app.graph.builder import build_graph

graph = build_graph()


__all__ = ["graph"]
