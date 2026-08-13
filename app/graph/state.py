"""
Conversation state shared across graph nodes.
"""

from __future__ import annotations

from typing import Annotated, NotRequired

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]

    # [claude] Which domain handled the most recent turn, set by the
    # supervisor. Optional, so the single-domain graphs — which have no
    # supervisor node — keep working unchanged.
    #
    # It is state rather than a local variable because it is worth seeing:
    # a wrong answer is often a routing mistake, and without this the trace
    # shows only which node ran, not what the classifier decided. It also
    # gives a follow-up turn a record of who answered last.
    route: NotRequired[str]


__all__ = ["AgentState"]
