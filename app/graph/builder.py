"""
Construction and wiring of the read-only MarQ domain graphs.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import StateGraph

from app.graph.agents.domain import (
    DEALS,
    DOMAINS,
    LEADS,
    Domain,
    build_domain_agent,
)
from app.graph.checkpointer import build_checkpointer
from app.graph.state import AgentState
from app.graph.supervisor import (
    FALLBACK_ROUTE,
    OUT_OF_SCOPE,
    choose_route,
    out_of_scope_message,
)
from app.llm.model import get_model

# [claude] Was an inline `10` in the node. A ReAct loop spends two steps per
# tool call, so 10 allowed roughly four calls — enough for one sql_query plus
# an analysis tool, but tight for anything that needed a second lookup.
# Named, and raised to leave headroom for a retry after a guard rejection.
MAX_AGENT_STEPS = 16


def make_domain_node(domain: Domain, model):
    """
    [claude] Build the graph node that runs one domain agent.

    Shared by the single-domain graphs and the supervisor graph, so the step
    ceiling and the GraphRecursionError handling exist once rather than being
    duplicated per agent — which is exactly how the second agent would have
    quietly lost the recursion guard.
    """

    agent = build_domain_agent(domain, model=model)

    async def domain_agent_node(state: AgentState):
        """Run the domain agent for the current conversation state."""

        try:
            result = await agent.ainvoke(
                {"messages": state["messages"]},
                config={"recursion_limit": MAX_AGENT_STEPS},
            )
        except GraphRecursionError:
            # [claude] The step ceiling was set but never caught, so hitting
            # it killed the whole run with an unhandled exception rather than
            # an answer. Degrade to a plain message instead: the loop is a bug
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
                ],
                "route": domain.name,
            }

        return {"messages": result["messages"], "route": domain.name}

    return domain_agent_node


def build_graph(domain: Domain = DEALS, checkpointer=None):
    """
    Build and compile the read-only graph for one domain.

    [claude] `domain` selects which agent the graph runs. It defaults to
    DEALS so existing callers are unchanged. The node is named after the
    domain, so a Studio trace shows which agent answered.

    checkpointer:
        None   use the default from build_checkpointer()
        False  compile without one — for the LangGraph API server, which
               provides its own persistence
        other  use the given checkpointer

    Architecture:

        Graph
          |
        Domain Agent  (tables + guard scope + prompt + tools)
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

    This graph talks to one domain only. build_supervisor_graph() routes
    across all registered domains.
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

    node_name = f"{domain.name}_agent"

    graph = StateGraph(AgentState)
    graph.add_node(node_name, make_domain_node(domain, model))

    graph.set_entry_point(node_name)
    graph.set_finish_point(node_name)

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


def build_supervisor_graph(checkpointer=None):
    """
    [claude] Build the routed graph across every registered domain.

        START
          |
        supervisor          one classification call, no tools
          |
          +-- deals ------> deals_agent  --> END
          +-- leads ------> leads_agent  --> END
          +-- out_of_scope -----------------> END   (direct reply)

    Routing happens once, up front, rather than through handoff tools. The
    domain agents are sealed — each one's guard permits only its own tables —
    so an agent cannot rescue a misrouted question by reaching into another
    domain's data. That makes the routing decision worth taking explicitly,
    where it is visible in the trace and assertable in an eval.

    Adding a domain to DOMAINS adds a node and a branch here automatically;
    only the supervisor prompt needs to learn the new category.
    """

    model = get_model()

    graph = StateGraph(AgentState)

    async def supervisor_node(state: AgentState):
        """Classify the turn. Adds no message — it only sets the route."""

        return {"route": await choose_route(model, state["messages"])}

    async def out_of_scope_node(state: AgentState):
        """Answer directly, without spending a database round-trip."""

        return {
            "messages": [out_of_scope_message()],
            "route": OUT_OF_SCOPE,
        }

    graph.add_node("supervisor", supervisor_node)
    graph.add_node(OUT_OF_SCOPE, out_of_scope_node)

    for name, domain in DOMAINS.items():
        graph.add_node(f"{name}_agent", make_domain_node(domain, model))

    graph.set_entry_point("supervisor")

    graph.add_conditional_edges(
        "supervisor",
        lambda state: state.get("route", FALLBACK_ROUTE),
        {
            **{name: f"{name}_agent" for name in DOMAINS},
            OUT_OF_SCOPE: OUT_OF_SCOPE,
        },
    )

    for name in DOMAINS:
        graph.set_finish_point(f"{name}_agent")
    graph.set_finish_point(OUT_OF_SCOPE)

    if checkpointer is None:
        checkpointer = build_checkpointer()

    return graph.compile(checkpointer=checkpointer)


def build_supervisor_studio_graph():
    """[claude] Supervisor entry point for the API server / Studio."""

    return build_supervisor_graph(checkpointer=False)


def build_studio_graph():
    """
    [claude] Deals Agent entry point for the LangGraph API server / Studio.

    Identical to build_graph() except that persistence is left to the
    server. Referenced from langgraph.json.
    """

    return build_graph(DEALS, checkpointer=False)


def build_leads_studio_graph():
    """[claude] Leads Agent entry point for the API server / Studio."""

    return build_graph(LEADS, checkpointer=False)


__all__ = [
    "MAX_AGENT_STEPS",
    "build_graph",
    "build_leads_studio_graph",
    "build_studio_graph",
    "build_supervisor_graph",  # [claude]
    "build_supervisor_studio_graph",  # [claude]
    "make_domain_node",  # [claude]
]
