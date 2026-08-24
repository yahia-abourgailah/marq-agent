"""
[claude] What reaches the model, and what must not.

From the 24 August review. `messages` was doing four unrelated jobs at once
— model context, UI trace, the `tools_used` record, and cross-specialist
memory — and the review found the same conflation surfacing as three
separate defects:

    M1  a `sql_query` payload is capped at 20,000 characters and
        `add_messages` accumulates, so four or five data turns filled the
        context window. Because the state is checkpointed, the oversized
        history became the thread's permanent state and every later turn
        failed the same way. The conversation could not be recovered.

    A1  the research agent — the one holding a tool that sends text to a
        third party — read CRM rows out of that same history, restrained
        only by a prompt rule.

    M2  checkpoint blobs accumulated raw CRM rows for the life of every
        conversation.

The fix is one split: a transcript the model reads, and a trace of tool
*names* for the UI. These hold that line. They are deliberately about the
channel rather than about any one agent, because the leak was never a
property of the research agent — it was a property of the channel every
agent shares.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.graph.agents.domain import DEALS
from app.graph.builder import make_domain_node, tool_names
from app.graph.state import collect_trace
from app.graph.supervisor import _conversation

CRM_ROWS = "client_name,unit_no\nA. Ibrahim,A-1204\nM. Farouk,B-0907"


class ToolUsingAgent:
    """An agent that calls a tool and gets rows back, like the real one."""

    def __init__(self, payload: str = CRM_ROWS) -> None:
        self.payload = payload
        self.seen: list = []

    async def ainvoke(self, state, config=None, context=None, **kwargs):
        self.seen.append(list(state["messages"]))

        return {
            "messages": [
                *state["messages"],
                AIMessage(
                    content="",
                    tool_calls=[{"name": "sql_query", "args": {}, "id": "c1"}],
                ),
                ToolMessage(content=self.payload, tool_call_id="c1"),
                AIMessage(content="There are 315 deals in total."),
            ]
        }


def node_with(agent, monkeypatch, collect=False):
    monkeypatch.setattr(
        "app.graph.builder.build_domain_agent", lambda *a, **k: agent
    )
    return make_domain_node(DEALS, model=object(), collect=collect)


def dump(state) -> str:
    """Everything a later model call could read out of this state."""

    return " ".join(
        str(getattr(m, "content", m)) for m in (state.get("messages") or [])
    )


# ============================================================
# M1 / M2 — payloads do not enter shared state
# ============================================================


@pytest.mark.asyncio
async def test_a_tool_payload_never_reaches_the_transcript(monkeypatch):
    """
    The single-domain graph. `result["messages"]` used to be returned
    wholesale, which wrote the agent's working — tool calls and their
    results — into the channel every later turn reloads.
    """

    node = node_with(ToolUsingAgent(), monkeypatch)

    result = await node({"messages": [HumanMessage(content="how many deals?")]})

    assert "A-1204" not in dump(result), "CRM rows reached the transcript"
    assert not any(
        isinstance(m, ToolMessage) for m in result["messages"]
    ), "a ToolMessage reached the transcript"
    assert result["messages"][-1].content == "There are 315 deals in total."


@pytest.mark.asyncio
async def test_a_tool_payload_never_reaches_the_findings_path(monkeypatch):
    """The supervisor graph's collect mode, which had the same shape."""

    node = node_with(ToolUsingAgent(), monkeypatch, collect=True)

    result = await node({"messages": [HumanMessage(content="how many deals?")]})

    assert "messages" not in result, (
        "a specialist wrote to the shared transcript; only synthesise should"
    )
    assert "A-1204" not in str(result), "CRM rows reached shared state"


@pytest.mark.asyncio
async def test_the_trace_carries_names_and_nothing_else(monkeypatch):
    """
    A tool pill needs the tool's name. It never needed its result, and that
    distinction is the whole fix.
    """

    node = node_with(ToolUsingAgent(), monkeypatch, collect=True)

    result = await node({"messages": [HumanMessage(content="q")]})

    assert result["trace"] == ["sql_query"]
    assert all(isinstance(name, str) for name in result["trace"])


@pytest.mark.asyncio
async def test_the_transcript_does_not_grow_with_the_working(monkeypatch):
    """
    [claude] The permanence is what made M1 HIGH rather than MEDIUM.

    Ten data turns used to cost ten tool payloads of history, carried
    forever in a checkpointed channel. This asserts the growth is bounded
    by the *answers*, which is what a transcript is for.
    """

    agent = ToolUsingAgent()
    node = node_with(agent, monkeypatch)

    history: list = []

    for i in range(10):
        history.append(HumanMessage(content=f"question {i}"))
        result = await node({"messages": history})
        history.extend(result["messages"])

    assert "A-1204" not in " ".join(str(m.content) for m in history)
    # Ten questions and ten answers. Nothing else.
    assert len(history) == 20


@pytest.mark.asyncio
async def test_the_agent_is_not_handed_a_previous_turn_s_rows(monkeypatch):
    """
    A1, stated as the channel property it actually is: whatever a
    specialist is given must not contain an earlier specialist's tool
    output. Asserted on what the agent *receives*, not on what it returns.
    """

    agent = ToolUsingAgent()
    node = node_with(agent, monkeypatch)

    history = [HumanMessage(content="how many deals?")]
    first = await node({"messages": history})
    history.extend(first["messages"])
    history.append(HumanMessage(content="and what is the market doing?"))

    await node({"messages": history})

    handed_over = " ".join(
        str(getattr(m, "content", m)) for m in agent.seen[-1]
    )

    assert "A-1204" not in handed_over, (
        "the second turn was handed the first turn's CRM rows"
    )


def test_the_trace_is_reset_by_the_same_signal_as_findings():
    """Checkpointed and concatenating, so it needs an answer to 'when does
    this end' — the lesson state.py already records for `findings`."""

    assert collect_trace(["sql_query"], ["make_chart"]) == [
        "sql_query",
        "make_chart",
    ]
    assert collect_trace(["sql_query"], None) == []


def test_tool_names_reads_only_names():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "sql_query", "args": {"sql": "SELECT 1"}, "id": "a"}],
        ),
        ToolMessage(content=CRM_ROWS, tool_call_id="a"),
    ]

    assert tool_names(messages) == ["sql_query"]


# ============================================================
# A2 — routing history is filtered before it is sliced
# ============================================================


def test_a_tool_heavy_turn_does_not_erase_the_previous_exchange():
    """
    [claude] The bug the order caused.

    `messages[-limit:]` took the last six *raw* messages and only then
    dropped tool traffic, so a turn with several tool calls left the
    classifier with the current question and nothing else — and a bare
    follow-up with no history falls through to the `deals` fallback.
    Workspace turns run to 28 steps, so reconciliations were exactly the
    turns after which follow-up routing lost its context.
    """

    history = [
        HumanMessage(content="how many contracted deals?"),
        AIMessage(content="There are 225 contracted deals."),
        HumanMessage(content="reconcile my sheet against the CRM"),
    ]
    # A tool-heavy turn: eight messages of working, all of it dropped.
    for i in range(4):
        history.append(
            AIMessage(
                content="",
                tool_calls=[{"name": "sql_query", "args": {}, "id": f"c{i}"}],
            )
        )
        history.append(ToolMessage(content=CRM_ROWS, tool_call_id=f"c{i}"))
    history.append(AIMessage(content="13 of 17 rows match."))
    history.append(HumanMessage(content="and how many of those closed?"))

    kept = _conversation(history, limit=6)

    assert [m["role"] for m in kept][-1] == "user"
    assert len(kept) > 1, "the follow-up was left with no history at all"
    assert any("225 contracted" in m["content"] for m in kept), (
        "the previous exchange was erased by tool traffic"
    )
    assert not any(CRM_ROWS in m["content"] for m in kept)


def test_the_limit_counts_exchanges_not_raw_messages():
    history: list = []
    for i in range(10):
        history.append(HumanMessage(content=f"q{i}"))
        history.append(AIMessage(content=f"a{i}"))

    kept = _conversation(history, limit=6)

    assert len(kept) == 6
    assert kept[0]["content"] == "q7"


# ============================================================
# A3 — third-party text does not arrive with system authority
# ============================================================


@pytest.mark.asyncio
async def test_specialist_answers_reach_synthesis_as_user_role(monkeypatch):
    """
    [claude] `parts` contains the research specialist's answer, which is
    derived from pages written by strangers. Carrying it as a
    `SystemMessage` gave that text the same standing as our own
    instructions, at the one node that writes the user-visible answer.

    Asserted on the actual call, because the distinction is invisible in
    the output: a merge built from a system-role block and one built from a
    user-role block look identical until someone plants an instruction.
    """

    from langgraph.checkpoint.memory import InMemorySaver

    import app.graph.builder as builder

    calls: list = []

    class FakeModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages, **kwargs):
            calls.append(list(messages))
            text = " ".join(
                str(m.get("content", "")) if isinstance(m, dict)
                else str(getattr(m, "content", ""))
                for m in messages
            )
            if "Reply with one word" in text:
                return AIMessage(content="deals, research")
            return AIMessage(content="an answer")

    monkeypatch.setattr(builder, "get_model", lambda: FakeModel())
    monkeypatch.setattr(
        "app.graph.builder.build_domain_agent",
        lambda *a, **k: ToolUsingAgent(),
    )

    graph = builder.build_supervisor_graph(checkpointer=InMemorySaver())
    await graph.ainvoke(
        {"messages": [HumanMessage(content="ours versus the market?")]},
        config={"configurable": {"thread_id": "a3"}},
    )

    # The synthesis call is the one carrying the specialist block.
    synthesis = [
        msgs for msgs in calls
        if any("SPECIALIST ANSWERS" in str(getattr(m, "content", "")) for m in msgs)
    ]

    assert synthesis, "no synthesis call carried the specialist block"

    carrier = [
        m for m in synthesis[-1]
        if "SPECIALIST ANSWERS" in str(getattr(m, "content", ""))
    ][0]

    assert carrier.type == "human", (
        f"specialist answers arrived as {carrier.type!r} — third-party text "
        "must not carry system authority"
    )


# ============================================================
# The residual: answers are CRM data too
# ============================================================


@pytest.mark.asyncio
async def test_a_toolless_specialist_does_not_read_another_s_answers(
    monkeypatch,
):
    """
    [claude] The narrower half of A1, found by the remediation review after
    the transcript split closed the wider one.

    Removing tool payloads from `messages` removed the raw rows. It did not
    remove CRM data, because the *answers* contain it: a reply to "who are
    our top clients by area" is prose naming clients, and it lives in the
    transcript every specialist is handed on the next turn.

    So the research agent — the one holding a tool that sends text to a
    third party — could still read a client name out of history, with only
    a prompt rule stopping it reaching a Tavily query. Smaller than raw
    rows, same mechanism, same mitigation.

    Asserted on what the agent *receives*, because that is the property.
    What it chooses to do with it is the prompt's business.
    """

    from langgraph.checkpoint.memory import InMemorySaver

    import app.graph.builder as builder

    seen: list = []

    class FakeModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages, **kwargs):
            text = " ".join(
                str(m.get("content", "")) if isinstance(m, dict)
                else str(getattr(m, "content", ""))
                for m in messages
            )

            if "Reply with one word" in text:
                return AIMessage(content="research")

            # Everything the research specialist was handed.
            seen.append(text)

            return AIMessage(content="The market is growing.")

    monkeypatch.setattr(builder, "get_model", lambda: FakeModel())
    monkeypatch.setattr(
        "app.graph.builder.build_domain_agent", lambda *a, **k: ToolUsingAgent()
    )

    graph = builder.build_supervisor_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "answers-case"}}

    # Turn one: a deals answer naming a client, planted directly in the
    # transcript the way synthesise would write it.
    await graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="who are our top clients by area?"),
                AIMessage(
                    content="A. Ibrahim holds the largest area at 412 sqm.",
                    additional_kwargs={"specialists": ["deals"]},
                ),
                HumanMessage(content="what is the market outlook?"),
            ]
        },
        config=config,
    )

    assert seen, "the research specialist never ran"

    handed_over = seen[-1]

    assert "A. Ibrahim" not in handed_over, (
        "the research agent was handed a client name out of the deals "
        "specialist's answer"
    )
    # The user's own question survives, so a follow-up still resolves.
    assert "market outlook" in handed_over


def test_a_specialist_still_reads_its_own_prior_answers():
    """
    The other half of the rule. Scoping history is only correct if it keeps
    what the specialist legitimately needs — "and last year?" after its own
    answer has to still resolve, or this trades a small leak for a broken
    follow-up.
    """

    from app.graph.builder import own_history_only

    history = [
        HumanMessage(content="what is the market outlook?"),
        AIMessage(
            content="The market grew 12% last quarter.",
            additional_kwargs={"specialists": ["research"]},
        ),
        HumanMessage(content="and the year before?"),
    ]

    kept = own_history_only(history, "research")

    assert [m.content for m in kept] == [
        "what is the market outlook?",
        "The market grew 12% last quarter.",
        "and the year before?",
    ]


def test_a_merged_answer_is_withheld_from_both_contributors():
    """
    [claude] A two-specialist reply holds both halves in one message and
    cannot be separated afterwards, so it is attributable to neither. The
    research half of it is research's own words — and it arrives fused to
    the deals figures, which is precisely what must not travel.
    """

    from app.graph.builder import own_history_only

    merged = AIMessage(
        content="A. Ibrahim holds 412 sqm. The wider market grew 12%.",
        additional_kwargs={"specialists": ["deals", "research"]},
    )

    for who in ("deals", "research"):
        assert own_history_only([merged], who) == []


def test_an_unattributed_answer_is_withheld():
    """
    Fails closed, which is what makes an old checkpoint safe to replay.
    Every message written before attribution existed carries none, and
    those are exactly the ones holding unfiltered CRM prose.
    """

    from app.graph.builder import own_history_only

    legacy = AIMessage(content="A. Ibrahim holds the largest area.")

    assert own_history_only([legacy], "research") == []
    assert own_history_only([legacy], "deals") == []


def test_domain_specialists_are_not_scoped():
    """
    [claude] Scoping applies to the toolless specialists only, and that is
    deliberate rather than an oversight.

    A domain agent holds `sql_query`; it can fetch any row its guard
    permits whenever it likes, so withholding an earlier answer protects
    nothing and would break every cross-turn follow-up the routing rules
    depend on. The property being defended is egress, and only the research
    agent has a tool that leaves the building.
    """

    import inspect

    from app.graph import builder

    source = inspect.getsource(builder.make_domain_node)

    assert "own_history_only" not in source, (
        "domain nodes must keep the full transcript — see the docstring"
    )
