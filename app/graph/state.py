"""
Conversation state shared across graph nodes.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired

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

    # [claude] Which employee the question is being asked on behalf of.
    #
    # Travels the same path as workspace_id and for the same reason: a model
    # that could name its own requester could name someone else's, so it is
    # never a tool argument. Reaches SQLExecutor, which publishes it as
    # `app.requester_id` for row-level security to filter on.
    #
    # Optional, and absent means unset rather than empty — a policy can tell
    # the difference and refuse, where an empty string would silently match
    # nothing and look like it was working.
    requester_id: NotRequired[str]

    # [claude] Which specialists this turn needs, chosen by the supervisor.
    #
    # `route` above holds the primary one and stays exactly as it was, so
    # every existing caller, eval and trace keeps working. This is the whole
    # list, because a question can legitimately need two — "how do our
    # cancellations compare with the market" is a deals question and a
    # research question, and answering only half of it looks like a complete
    # answer.
    plan: NotRequired[list[str]]

    # [claude] What each specialist came back with, before they are merged.
    #
    # Needs a reducer because the specialists run **in parallel**. Without
    # one, LangGraph raises `InvalidUpdateError` on two nodes writing the
    # same channel in a single step — and with a last-write-wins field you
    # would instead get one specialist's answer silently discarded, which is
    # the same shape as every other bug this project has found: a complete
    # looking answer that quietly dropped half the question.
    #
    # `operator.add` on lists concatenates, so order follows completion
    # rather than the plan. The synthesis step sorts by domain name so the
    # merged answer does not reorder itself run to run.
    findings: Annotated[list[dict[str, Any]], operator.add]


__all__ = ["AgentState"]
