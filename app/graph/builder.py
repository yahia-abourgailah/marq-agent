"""
Construction and wiring of the read-only MarQ domain graphs.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import StateGraph

from app.graph.agents.domain import (
    DEALS,
    DOMAINS,
    LEADS,
    WORKSPACE,
    Domain,
    build_domain_agent,
)
from app.graph.agents.general import GENERAL_AGENT_SYSTEM_PROMPT
from app.graph.checkpointer import build_checkpointer
from app.graph.state import AgentState
from app.graph.supervisor import (
    FALLBACK_ROUTE,
    GENERAL_ROUTE,
    choose_route,
    out_of_scope_message,
)
from app.llm.model import get_model
from app.tools.workspace import WorkspaceContext

# [claude] Was an inline `10` in the node. A ReAct loop spends two steps per
# tool call, so 10 allowed roughly four calls — enough for one sql_query plus
# an analysis tool, but tight for anything that needed a second lookup.
# Named, and raised to leave headroom for a retry after a guard rejection.
MAX_AGENT_STEPS = 16


def make_domain_node(domain: Domain, model, workspace_service=None):
    """
    [claude] Build the graph node that runs one domain agent.

    Shared by the single-domain graphs and the supervisor graph, so the step
    ceiling and the GraphRecursionError handling exist once rather than being
    duplicated per agent — which is exactly how the second agent would have
    quietly lost the recursion guard.

    `workspace_service` is passed through for domains that read uploaded
    files, mirroring how `database` is already injectable further down. The
    workspace evals need it: they build a throwaway workspace in a temporary
    directory, and without a way to inject it the graph would read whatever
    the developer happens to have uploaded locally.
    """

    agent = build_domain_agent(
        domain, model=model, workspace_service=workspace_service
    )

    # [claude] Per-domain ceiling, falling back to the shared default. A
    # reconciliation legitimately needs more steps than a single count.
    step_limit = domain.max_steps or MAX_AGENT_STEPS

    async def domain_agent_node(state: AgentState):
        """Run the domain agent for the current conversation state."""

        try:
            result = await agent.ainvoke(
                {"messages": state["messages"]},
                config={"recursion_limit": step_limit},
                # [claude] The workspace id travels as runtime context, not
                # as part of the message state, so it reaches the tools
                # without ever being visible to the model as something it
                # could set. Domains without workspace tools ignore it.
                context=WorkspaceContext(
                    workspace_id=state.get("workspace_id"),
                    requester_id=state.get("requester_id"),
                ),
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


def build_graph(domain: Domain = DEALS, checkpointer=None, workspace_service=None):
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
    graph.add_node(
        node_name, make_domain_node(domain, model, workspace_service)
    )

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

    async def general_node(state: AgentState):
        """
        [claude] Greetings, capability questions, general conversation.

        Was a canned sentence, which meant "hi" and "what can you do?" — the
        two things people actually open a chat window with — both got a
        refusal. That reads as broken rather than scoped.

        It is a plain model call with a system prompt, deliberately **not**
        `build_domain_agent`: that would hand it a `sql_query` tool, and this
        is the agent most likely to be asked to do something odd. It holds no
        keys at all, so there is nothing for a persuasive user to reach.

        A failure here degrades to the fixed sentence rather than taking the
        turn down, because the whole point is answering a greeting.
        """

        messages = [
            SystemMessage(content=GENERAL_AGENT_SYSTEM_PROMPT),
            *state["messages"],
        ]

        try:
            reply = await model.ainvoke(messages)
        except Exception:
            return {
                "messages": [out_of_scope_message()],
                "route": GENERAL_ROUTE,
            }

        return {"messages": [reply], "route": GENERAL_ROUTE}

    graph.add_node("supervisor", supervisor_node)
    graph.add_node(GENERAL_ROUTE, general_node)

    for name, domain in DOMAINS.items():
        graph.add_node(f"{name}_agent", make_domain_node(domain, model))

    graph.set_entry_point("supervisor")

    graph.add_conditional_edges(
        "supervisor",
        lambda state: state.get("route", FALLBACK_ROUTE),
        {
            **{name: f"{name}_agent" for name in DOMAINS},
            GENERAL_ROUTE: GENERAL_ROUTE,
        },
    )

    for name in DOMAINS:
        graph.set_finish_point(f"{name}_agent")
    graph.set_finish_point(GENERAL_ROUTE)

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


def build_workspace_studio_graph():
    """
    [claude] Workspace Agent entry point for the API server / Studio.

    Note that a Studio run has no workspace attached unless `workspace_id` is
    set in the input state, so the file tools will correctly report that
    nothing is uploaded. That is the honest behaviour rather than a bug —
    there is no ambient workspace to fall back to, by design.
    """

    return build_graph(WORKSPACE, checkpointer=False)


__all__ = [
    "MAX_AGENT_STEPS",
    "build_graph",
    "build_leads_studio_graph",
    "build_studio_graph",
    "build_supervisor_graph",  # [claude]
    "build_supervisor_studio_graph",  # [claude]
    "build_workspace_studio_graph",  # [claude]
    "make_domain_node",  # [claude]
]
