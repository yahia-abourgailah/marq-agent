"""
[claude] How a turn ended, and whether anything can tell.

`make_domain_node` degraded a `GraphRecursionError` into a friendly
`AIMessage` and logged nothing. An out-of-steps run therefore looked exactly
like a real answer to everything downstream — prose in the body, 200 on the
response, no log line saying otherwise. That was tolerable while a human read
a CLI trace and stopped being tolerable when a front end was attached.

Three things are held here, and the third is the one that would break
silently:

1.  Every ending produces a reason, and the reason is decided by control
    flow rather than by reading the answer text.
2.  A turn that ran two specialists reports the *worst* of them, because a
    half answer that reports `completed` is the failure shape this project
    keeps finding.
3.  Exactly one node writes the channel. The specialists run in parallel, so
    two of them writing it is `InvalidUpdateError` — the collision `findings`
    already carries a reducer for.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

import app.graph.builder as builder
from app.graph.agents.domain import DEALS, DOMAINS
from app.graph.builder import make_domain_node
from app.graph.state import (
    COMPLETED,
    ERROR,
    OUT_OF_STEPS,
    REFUSED,
    STOP_REASONS,
    resolve_stop_reason,
)


class StubAgent:
    """A domain agent that answers, or fails in a chosen way."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises

    async def ainvoke(self, state, config=None, context=None, **kwargs):
        if self.raises is not None:
            raise self.raises

        return {"messages": [*state["messages"], AIMessage(content="answer")]}


def node_with(domain, agent, monkeypatch, collect=False):
    monkeypatch.setattr(
        "app.graph.builder.build_domain_agent",
        lambda *args, **kwargs: agent,
    )
    return make_domain_node(domain, model=object(), collect=collect)


# ============================================================
# The vocabulary
# ============================================================


def test_the_four_reasons_are_the_only_ones():
    """
    Deliberately closed. A fifth value added casually would reach the API
    response and the logs as a status nobody defined, and `stop_reason_from`
    would drop it on the floor rather than pass it on.
    """

    assert set(STOP_REASONS) == {COMPLETED, OUT_OF_STEPS, REFUSED, ERROR}


# ============================================================
# Resolving one reason from several specialists
# ============================================================


def finding(specialist, answer, reason=COMPLETED):
    return {"specialist": specialist, "answer": answer, "stop_reason": reason}


def test_one_specialist_that_answered_is_completed():
    assert resolve_stop_reason([finding("deals", "315 deals")]) == COMPLETED


def test_a_specialist_that_died_outranks_one_that_answered():
    """
    [claude] The whole reason worst-wins is the rule.

    The reader still gets the deals half — `synthesise` reports what it has,
    which is correct. But the turn did not answer the question that was
    asked, and reporting `completed` would make it indistinguishable from
    one that did.
    """

    reasons = resolve_stop_reason(
        [finding("deals", "315 deals"), finding("research", "", ERROR)]
    )

    assert reasons == ERROR


def test_running_out_of_steps_outranks_answering():
    assert (
        resolve_stop_reason(
            [
                finding("deals", "315 deals"),
                finding("workspace", "I ran out of steps", OUT_OF_STEPS),
            ]
        )
        == OUT_OF_STEPS
    )


def test_a_dead_specialist_outranks_one_that_ran_out_of_steps():
    assert (
        resolve_stop_reason(
            [finding("deals", "", ERROR), finding("workspace", "x", OUT_OF_STEPS)]
        )
        == ERROR
    )


def test_nothing_answered_is_refused():
    """
    `refused` is structural, not sniffed out of prose. Nothing came back
    with anything in it, so synthesise fell through to the canned reply.
    """

    assert resolve_stop_reason([]) == REFUSED
    assert resolve_stop_reason([finding("general", "   ")]) == REFUSED


def test_an_empty_answer_from_a_failure_is_error_not_refused():
    """
    [claude] The distinction that makes the field worth having.

    A specialist that raises leaves an empty answer behind, which is
    indistinguishable from one that had nothing to say. The node records
    the reason at the point it catches the exception, so the two do not
    collapse into each other here.
    """

    assert resolve_stop_reason([finding("research", "", ERROR)]) == ERROR


def test_a_finding_without_a_reason_is_assumed_completed():
    """Old checkpoints, and any future node that forgets to say."""

    assert resolve_stop_reason([{"specialist": "deals", "answer": "x"}]) == COMPLETED


# ============================================================
# The node reports its own ending
# ============================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
async def test_running_out_of_steps_is_recorded_not_just_apologised_for(
    domain, monkeypatch
):
    """
    The apology already existed. What did not was any way to tell it apart
    from an answer.
    """

    node = node_with(
        domain, StubAgent(raises=GraphRecursionError("limit")), monkeypatch
    )

    result = await node({"messages": ["q"]})

    assert result["stop_reason"] == OUT_OF_STEPS
    assert "out of steps" in str(result["messages"][-1].content)


@pytest.mark.asyncio
async def test_a_normal_turn_is_completed(monkeypatch):
    node = node_with(DEALS, StubAgent(), monkeypatch)

    assert (await node({"messages": ["q"]}))["stop_reason"] == COMPLETED


@pytest.mark.asyncio
async def test_in_collect_mode_the_reason_travels_inside_the_finding(monkeypatch):
    """
    [claude] Not as a top-level key, and this is the design constraint.

    In the supervisor graph the specialists run in one superstep. A plain
    channel written by two of them is `InvalidUpdateError`; `findings`
    already has a reducer for exactly that, so the reason rides along inside
    it and one node downstream resolves the turn's answer.
    """

    node = node_with(DEALS, StubAgent(), monkeypatch, collect=True)

    result = await node({"messages": ["q"]})

    assert result["findings"][0]["stop_reason"] == COMPLETED
    assert "stop_reason" not in result, (
        "a specialist must not write the channel directly — two of them "
        "running in parallel would collide"
    )


@pytest.mark.asyncio
async def test_collect_mode_reports_out_of_steps_inside_the_finding(monkeypatch):
    node = node_with(
        DEALS, StubAgent(raises=GraphRecursionError("limit")), monkeypatch, collect=True
    )

    result = await node({"messages": ["q"]})

    assert result["findings"][0]["stop_reason"] == OUT_OF_STEPS
    assert "stop_reason" not in result


# ============================================================
# End to end, through the graph that ships
# ============================================================


class FakeModel:
    """
    The supervisor and the specialists, faked.

    `bind_tools` is required because the toolless specialists carry
    `make_chart` and therefore go through `create_agent`.
    """

    def __init__(self, plan: str = "general", fail_specialists: bool = False) -> None:
        self.plan = plan
        self.fail_specialists = fail_specialists

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        def content_of(message):
            if isinstance(message, dict):
                return str(message.get("content", ""))
            return str(getattr(message, "content", ""))

        text = " ".join(content_of(m) for m in messages)

        # The supervisor's prompt is the one asking for a single word.
        if "Reply with one word" in text:
            return AIMessage(content=self.plan)

        if self.fail_specialists:
            raise RuntimeError("the specialist's model call failed")

        return AIMessage(content="a real answer")


def supervisor_graph(monkeypatch, model, agent=None):
    """The shipping supervisor graph, with the model and agents stubbed."""

    monkeypatch.setattr(builder, "get_model", lambda: model)

    if agent is not None:
        monkeypatch.setattr(
            "app.graph.builder.build_domain_agent",
            lambda *args, **kwargs: agent,
        )

    return builder.build_supervisor_graph(checkpointer=InMemorySaver())


async def run(graph, question, thread="stop-reason"):
    return await graph.ainvoke(
        {"messages": [HumanMessage(content=question)]},
        config={"configurable": {"thread_id": thread}},
    )


@pytest.mark.asyncio
async def test_a_turn_that_answered_reports_completed(monkeypatch):
    graph = supervisor_graph(monkeypatch, FakeModel(plan="general"))

    result = await run(graph, "hello")

    assert result["stop_reason"] == COMPLETED
    assert result["messages"][-1].content == "a real answer"


@pytest.mark.asyncio
async def test_a_specialist_that_died_reports_error(monkeypatch):
    """
    The turn still returns a reply — a dead specialist must not take the
    request down. What changes is that the reply no longer claims to be an
    answer.
    """

    graph = supervisor_graph(
        monkeypatch, FakeModel(plan="general", fail_specialists=True)
    )

    result = await run(graph, "hello")

    assert result["stop_reason"] == ERROR
    assert result["messages"][-1].content


@pytest.mark.asyncio
async def test_an_out_of_steps_specialist_reports_out_of_steps(monkeypatch):
    """
    [claude] The case the whole field exists for, through the real graph.

    The user sees an apology. Before this, so did everything else — the
    response body, the log, and any monitor counting them.
    """

    graph = supervisor_graph(
        monkeypatch,
        FakeModel(plan="deals"),
        agent=StubAgent(raises=GraphRecursionError("limit")),
    )

    result = await run(graph, "how many deals are there")

    assert result["stop_reason"] == OUT_OF_STEPS
    assert "out of steps" in str(result["messages"][-1].content)


@pytest.mark.asyncio
async def test_two_specialists_in_one_superstep_do_not_collide(monkeypatch):
    """
    [claude] The regression test for the design constraint.

    `deals, research` fans out to two nodes in one superstep. If either
    wrote `stop_reason` directly, LangGraph would raise `InvalidUpdateError`
    here — which is why the reason rides inside `findings` and is resolved
    downstream. This test fails loudly the moment someone "simplifies" that
    by writing the channel from the specialist.
    """

    graph = supervisor_graph(
        monkeypatch, FakeModel(plan="deals, research"), agent=StubAgent()
    )

    result = await run(graph, "how do our cancellations compare with the market")

    assert sorted(f["specialist"] for f in result["findings"]) == [
        "deals",
        "research",
    ]
    assert result["stop_reason"] == COMPLETED


@pytest.mark.asyncio
async def test_the_reason_does_not_leak_between_turns(monkeypatch):
    """
    [claude] The bug class this graph has already shipped once.

    `findings` accumulated across turns because it was checkpointed with a
    concatenating reducer, and turn two answered with turn one's results.
    `stop_reason` is checkpointed too, so a turn that ran out of steps must
    not leave `out_of_steps` sitting there for the next question that
    succeeds.
    """

    failing = StubAgent(raises=GraphRecursionError("limit"))
    model = FakeModel(plan="deals")

    monkeypatch.setattr(builder, "get_model", lambda: model)
    monkeypatch.setattr(
        "app.graph.builder.build_domain_agent",
        lambda *args, **kwargs: failing,
    )

    graph = builder.build_supervisor_graph(checkpointer=InMemorySaver())

    first = await run(graph, "a question that loops", thread="shared")
    assert first["stop_reason"] == OUT_OF_STEPS

    # The next turn on the same thread succeeds.
    failing.raises = None
    second = await run(graph, "a question that works", thread="shared")

    assert second["stop_reason"] == COMPLETED
