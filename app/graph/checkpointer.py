"""
LangGraph checkpointer selection.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver


def build_checkpointer() -> InMemorySaver:
    """
    Build the LangGraph checkpointer used for development.

    InMemorySaver keeps checkpoints in the running process.
    It is suitable for local development and testing.

    Production should use a persistent PostgreSQL-backed checkpointer.
    """

    return InMemorySaver()


__all__ = ["build_checkpointer"]
