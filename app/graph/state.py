"""
Conversation state shared across graph nodes.
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


def collect_findings(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    [claude] Accumulate findings within a turn; clear them between turns.

    This was `operator.add`, which accumulates forever. `findings` is
    checkpointed, so turn two saw turn one's findings still sitting there,
    concluded that two specialists had run, and merged the previous
    answer into the new one — "okay" was answered with a list of Egyptian
    property developers left over from the question before it.

    A plain reducer cannot express "start again", so `None` is the reset
    signal and the supervisor sends it at the top of every turn. It is the
    one value a node would never append.

    The bug is worth remembering rather than just fixing: `messages`
    accumulating across turns is exactly what you want, and `findings`
    looking identical made the difference invisible. Anything checkpointed
    with a concatenating reducer needs an answer to "when does this end".
    """

    if incoming is None:
        return []

    return [*(existing or []), *incoming]


# ============================================================
# How a turn ended
# ============================================================
#
# [claude] `make_domain_node` degraded a GraphRecursionError into a friendly
# AIMessage and logged nothing, so an out-of-steps run was indistinguishable
# from a real answer to anything watching. That was tolerable when a human
# was reading a CLI trace and is not once a front end is attached: the
# operator could not tell "the agent ran out of steps" from "the agent
# answered", because both arrive as prose with a 200 beside it.
#
# Four values, and deliberately only four. Each one is decided by control
# flow — an exception caught, a branch taken — never by reading the answer
# text. Inferring "the agent refused" from prose is exactly the confident
# guess this codebase keeps finding bugs in.

COMPLETED = "completed"
OUT_OF_STEPS = "out_of_steps"
REFUSED = "refused"
ERROR = "error"

# Worst first. `resolve_stop_reason` walks this order, so a turn where one
# specialist answered and the other died reports the death.
STOP_REASONS = (ERROR, OUT_OF_STEPS, REFUSED, COMPLETED)


def resolve_stop_reason(findings: list[dict[str, Any]] | None) -> str:
    """
    [claude] One reason for a turn that may have run two specialists.

    Worst-wins, and that is the whole decision. A turn where the deals
    specialist answered and the research specialist raised has produced half
    an answer, and half an answer that reports itself as `completed` is the
    recurring failure shape this project keeps finding — something that
    looks whole and quietly dropped a piece. The operator should see
    `error`; the reader still gets the half that worked, because
    `synthesise` reports what it has.

    `refused` is not sniffed out of the answer text. It is the structural
    case: nothing came back with anything in it, so `synthesise` fell
    through to the canned out-of-scope reply. An empty answer caused by an
    exception is already labelled `error` by the node that caught it, and
    ERROR precedes REFUSED above, so the two do not get confused.
    """

    collected = findings or []
    reasons = {
        str(finding.get("stop_reason") or COMPLETED) for finding in collected
    }

    for reason in (ERROR, OUT_OF_STEPS):
        if reason in reasons:
            return reason

    if not any(str(finding.get("answer") or "").strip() for finding in collected):
        return REFUSED

    return COMPLETED


def collect_trace(
    existing: list[str] | None, incoming: list[str] | None
) -> list[str]:
    """
    [claude] Tool names for the turn in flight. Same shape as
    `collect_findings`, and reset by the same signal.

    Needs a reducer for the same reason `findings` does — two specialists
    write it in one superstep — and needs a reset for the same reason too:
    it is checkpointed, so without one, turn two would report turn one's
    tools as its own.
    """

    if incoming is None:
        return []

    return [*(existing or []), *incoming]


class AgentState(TypedDict):
    # [claude] The transcript the model reads. Questions and answers only.
    #
    # It used to carry the working as well — every `AIMessage` bearing a
    # tool call and every `ToolMessage` bearing its result — because the UI
    # wanted to draw a pill per tool. That put four unrelated jobs on one
    # channel, with different audiences, lifetimes and sensitivities, and
    # all three of the problems it caused were real:
    #
    #   *  A `sql_query` payload is capped at 20,000 characters, roughly
    #      5,000 tokens, and `add_messages` accumulates. Four or five data
    #      turns filled a 32k context; the overflow arrived as a provider
    #      error, was caught generically, and — because the state is
    #      checkpointed — became the thread's permanent state. Every later
    #      turn reloaded the same oversized history and failed the same
    #      way. The conversation could not be recovered.
    #
    #   *  The research agent holds the one tool that sends text to a third
    #      party and is documented as never seeing CRM rows. That was true
    #      of its tools and false of its context: it read `messages`, which
    #      held the rows an earlier deals turn had fetched.
    #
    #   *  Checkpoint blobs accumulated raw CRM rows — client names, unit
    #      numbers — for the life of every conversation, putting CRM
    #      retention rules on the conversation store.
    #
    # Rendering a tool pill never required the tool's *result* to be in the
    # model's context. It required the tool's *name*. So the names travel
    # in `trace` below and the payloads travel nowhere: they stay inside
    # the agent's own run, where the ReAct loop needs them, and are dropped
    # when it returns.
    messages: Annotated[list[AnyMessage], add_messages]

    # [claude] Which tools ran this turn, in order. Names only — never
    # arguments, never results. This is what the UI's tool pills and the
    # API's `tools_used` are built from.
    #
    # Reset every turn by the supervisor, so it stays the size of one turn
    # rather than growing with the conversation.
    trace: Annotated[list[str], collect_trace]

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
    # Concatenating, so order follows completion rather than the plan. The
    # synthesis step sorts by specialist name so a merged answer does not
    # reorder itself run to run.
    findings: Annotated[list[dict[str, Any]], collect_findings]

    # [claude] How the turn ended — one of STOP_REASONS above.
    #
    # Written by exactly one node per graph, which is not a style choice.
    # The specialists run in parallel, and two of them writing a plain
    # channel in one superstep is what LangGraph raises `InvalidUpdateError`
    # for — the same collision `findings` carries a reducer for. So a
    # specialist records its own outcome *inside its finding*, where the
    # reducer already handles the concurrency, and `synthesise` resolves the
    # turn's single reason downstream. The single-domain graphs have one
    # node and no such contention, so that node writes this directly.
    #
    # Optional, because absent is honest: a caller that did not go through
    # a graph capable of setting it should read None rather than a
    # confident `completed` nobody established.
    stop_reason: NotRequired[str]


__all__ = [
    "COMPLETED",  # [claude]
    "collect_trace",  # [claude]
    "ERROR",  # [claude]
    "OUT_OF_STEPS",  # [claude]
    "REFUSED",  # [claude]
    "STOP_REASONS",  # [claude]
    "AgentState",
    "resolve_stop_reason",  # [claude]
]
