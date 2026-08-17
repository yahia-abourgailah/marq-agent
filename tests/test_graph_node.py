"""
[claude] The domain node's own behaviour, independent of any model.

`make_domain_node` wraps every agent, so its two jobs — passing the workspace
context down, and degrading instead of crashing when the step ceiling is hit
— apply to all three domains. Neither was covered: the recursion branch is
what a user sees when something goes wrong, and it had never been executed
by a test.

The agent is a stub here. That is the point: this is about the wrapper.
"""

from __future__ import annotations

import pytest
from langgraph.errors import GraphRecursionError

from app.graph.agents.domain import DEALS, DOMAINS, WORKSPACE
from app.graph.builder import MAX_AGENT_STEPS, make_domain_node


class StubAgent:
    """Records how it was invoked, and can be told to fail."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.calls: list[dict] = []

    async def ainvoke(self, state, config=None, context=None, **kwargs):
        self.calls.append(
            {"state": state, "config": config or {}, "context": context}
        )

        if self.raises is not None:
            raise self.raises

        return {"messages": [*state["messages"], "answer"]}


def node_with(domain, agent, monkeypatch):
    monkeypatch.setattr(
        "app.graph.builder.build_domain_agent",
        lambda *args, **kwargs: agent,
    )
    return make_domain_node(domain, model=object())


# ============================================================
# Degrading instead of crashing
# ============================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS.values(), ids=lambda d: d.name)
async def test_running_out_of_steps_answers_instead_of_raising(
    domain, monkeypatch
):
    """
    Hitting the ceiling used to kill the whole run with an unhandled
    exception. Every domain must degrade to a message.
    """

    agent = StubAgent(raises=GraphRecursionError("limit"))
    node = node_with(domain, agent, monkeypatch)

    result = await node({"messages": ["q"]})

    assert result["route"] == domain.name

    answer = str(result["messages"][-1].content)
    assert "out of steps" in answer
    assert "more specific" in answer


@pytest.mark.asyncio
async def test_other_exceptions_are_not_swallowed(monkeypatch):
    """
    Only the step ceiling degrades. A genuine bug must still surface —
    catching everything here would hide it behind a friendly message.
    """

    agent = StubAgent(raises=ValueError("a real bug"))
    node = node_with(DEALS, agent, monkeypatch)

    with pytest.raises(ValueError, match="a real bug"):
        await node({"messages": ["q"]})


# ============================================================
# The step ceiling that gets applied
# ============================================================


@pytest.mark.asyncio
async def test_a_domain_without_its_own_ceiling_gets_the_default(monkeypatch):
    agent = StubAgent()
    node = node_with(DEALS, agent, monkeypatch)

    await node({"messages": ["q"]})

    assert agent.calls[0]["config"]["recursion_limit"] == MAX_AGENT_STEPS


@pytest.mark.asyncio
async def test_a_domain_with_its_own_ceiling_uses_it(monkeypatch):
    """
    The workspace reconciliation needs four tool calls before it can answer,
    which the default ceiling could not cover.
    """

    agent = StubAgent()
    node = node_with(WORKSPACE, agent, monkeypatch)

    await node({"messages": ["q"]})

    limit = agent.calls[0]["config"]["recursion_limit"]

    assert limit == WORKSPACE.max_steps
    assert limit > MAX_AGENT_STEPS


# ============================================================
# The workspace id reaches the agent as context
# ============================================================


@pytest.mark.asyncio
async def test_the_workspace_id_is_passed_as_runtime_context(monkeypatch):
    agent = StubAgent()
    node = node_with(WORKSPACE, agent, monkeypatch)

    await node({"messages": ["q"], "workspace_id": "ws-42"})

    assert agent.calls[0]["context"].workspace_id == "ws-42"


@pytest.mark.asyncio
async def test_a_missing_workspace_id_becomes_none_not_an_error(monkeypatch):
    """
    Most turns have no workspace. The context is still passed, carrying
    None, so the tools can report "nothing uploaded" rather than crash.
    """

    agent = StubAgent()
    node = node_with(WORKSPACE, agent, monkeypatch)

    await node({"messages": ["q"]})

    assert agent.calls[0]["context"].workspace_id is None


@pytest.mark.asyncio
async def test_the_workspace_id_never_enters_the_message_state(monkeypatch):
    """
    It travels as context precisely so the model cannot see or set it. If it
    leaked into messages, a prompt-injected file could name another
    workspace.
    """

    agent = StubAgent()
    node = node_with(WORKSPACE, agent, monkeypatch)

    await node({"messages": ["q"], "workspace_id": "ws-secret"})

    assert "ws-secret" not in str(agent.calls[0]["state"])


@pytest.mark.asyncio
async def test_the_node_records_which_domain_answered(monkeypatch):
    """A wrong answer is often a routing mistake, so the trace must say."""

    for domain in DOMAINS.values():
        agent = StubAgent()
        node = node_with(domain, agent, monkeypatch)

        result = await node({"messages": ["q"]})

        assert result["route"] == domain.name
