"""
Construction and wiring of the read-only MarQ Deals graph.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import StateGraph

from app.graph.agents.domain import DEALS, build_domain_agent
from app.graph.checkpointer import build_checkpointer
from app.graph.state import AgentState
from app.llm.model import get_model

# [claude] Was an inline `10` in the node. A ReAct loop spends two steps per
# tool call, so 10 allowed roughly four calls — enough for one sql_query plus
# an analysis tool, but tight for anything that needed a second lookup.
# Named, and raised to leave headroom for a retry after a guard rejection.
MAX_AGENT_STEPS = 16


def build_graph(checkpointer=None):
    """
    Build and compile the read-only MarQ Deals graph.

    [claude] checkpointer:
        None   use the default from build_checkpointer()
        False  compile without one — for the LangGraph API server, which
               provides its own persistence
        other  use the given checkpointer

    Architecture:

        Graph
          |
        Deals Agent  (one Domain: tables + prompt + tools)
          |
        +--------------------+
        |                    |
     sql_query          analysis tools
        |                    |
     SQL Agent               | arithmetic only,
        |                    | no database access
     SQL Guard               |
        |                    |
     SQL Executor            |
        |                    |
     PostgreSQL              |
        |                    |
        +---------+----------+
                  |
             Final Answer

    A second domain agent is another build_domain_agent() call and another
    node; see app/graph/agents/domain.py.
    """

    # ---------------------------------------------------------
    # Agents
    # ---------------------------------------------------------
    #
    # [claude] The SQL agent, guard, repository and tool wiring moved into
    # build_domain_agent(), which derives all of it from the Domain. It used
    # to be spelled out here, which meant a second agent would have to
    # duplicate it — and would silently share the guard's global allowlist.

    model = get_model()

    deals_agent = build_domain_agent(DEALS, model=model)

    # ---------------------------------------------------------
    # LangGraph
    # ---------------------------------------------------------

    graph = StateGraph(AgentState)

    async def deals_agent_node(state: AgentState):
        """
        Run the Deals Agent for the current conversation state.
        """

        try:
            result = await deals_agent.ainvoke(
                {
                    "messages": state["messages"],
                },
                config={
                    "recursion_limit": MAX_AGENT_STEPS,
                },
            )
        except GraphRecursionError:
            # [claude] The step ceiling was set but never caught, so hitting
            # it killed the whole run with an unhandled exception rather than
            # an answer. Any loop — a tool the agent keeps retrying, a
            # question it cannot resolve — surfaced to the user as a crash.
            #
            # Degrade to a plain message instead. The loop itself is a bug
            # worth fixing wherever it comes from, but it should not take the
            # request down with it.
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "I wasn't able to complete that request — I ran "
                            "out of steps while working on it. Try asking "
                            "for something more specific."
                        )
                    )
                ]
            }

        return {
            "messages": result["messages"],
        }

    graph.add_node(
        "deals_agent",
        deals_agent_node,
    )

    graph.set_entry_point("deals_agent")
    graph.set_finish_point("deals_agent")

    # ---------------------------------------------------------
    # Checkpointing
    # ---------------------------------------------------------
    #
    # [claude] `checkpointer` is now a parameter.
    #
    # When the graph runs under the LangGraph API server (`langgraph dev`,
    # which is what LangGraph Studio talks to), the server supplies its own
    # persistence. A checkpointer baked in at compile time competes with it,
    # so langgraph.json points at `build_studio_graph()` below, which
    # compiles without one.
    #
    # Everywhere else the default still applies, so nothing that already
    # called build_graph() changes behaviour.

    if checkpointer is None:
        checkpointer = build_checkpointer()

    return graph.compile(
        checkpointer=checkpointer,
    )


def build_studio_graph():
    """
    [claude] Entry point for the LangGraph API server / Studio.

    Identical to build_graph() except that persistence is left to the
    server. Referenced from langgraph.json.
    """

    return build_graph(checkpointer=False)


__all__ = ["build_graph", "build_studio_graph"]  # [claude] added studio entry
