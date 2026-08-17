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

    # [claude] Which set of uploaded files this conversation may read.
    #
    # Set by the caller when the conversation has a workspace, and passed
    # down to the workspace tools as LangGraph runtime context. It is state
    # rather than a tool argument for one reason: a model that could name its
    # own workspace could name someone else's. The tools take it from here
    # and nowhere else.
    #
    # Optional, so the single-domain graphs and every existing caller keep
    # working unchanged — absent simply means "no files attached", which the
    # workspace tools report as such.
    workspace_id: NotRequired[str]


__all__ = ["AgentState"]
