"""
Construction and wiring of the read-only MarQ domain graphs.
"""

from __future__ import annotations

import logging

from langchain.agents import create_agent
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
from app.graph.agents.research import RESEARCH_AGENT_SYSTEM_PROMPT
from app.graph.checkpointer import build_checkpointer
from app.graph.state import AgentState
from app.graph.supervisor import (
    FALLBACK_ROUTE,
    GENERAL_ROUTE,
    RESEARCH_ROUTE,
    choose_plan,
    out_of_scope_message,
)
from app.llm.model import get_model
from app.tools.web import build_web_tools
from app.tools.workspace import WorkspaceContext

# [claude] Was an inline `10` in the node. A ReAct loop spends two steps per
# tool call, so 10 allowed roughly four calls — enough for one sql_query plus
# an analysis tool, but tight for anything that needed a second lookup.
# Named, and raised to leave headroom for a retry after a guard rejection.
MAX_AGENT_STEPS = 16

logger = logging.getLogger("marq.graph")

# [claude] Specialists that are not Domains: no tables, no guard, no
# `sql_query`. They are the agents most exposed to being talked into
# something — one takes arbitrary chat, the other reads pages written by
# strangers — so they are the ones holding no keys.
_TOOLLESS = (RESEARCH_ROUTE, GENERAL_ROUTE)

SYNTHESIS_PROMPT = """\
Two specialists have each answered part of the user's question. Merge them
into one reply.

Keep every figure exactly as given. Do not recalculate, round, combine or
rephrase a number — those came from the database or from cited sources and
are already correct.

Write one coherent answer, not two stitched together. Make clear which part
is MarQ's own data and which is outside information, because the reader
needs to know what is measured and what is context.

If one specialist could not answer, say so briefly and give the half that
worked. Do not fill the gap yourself.

Be brief. No preamble, no restating the question.
"""


def make_domain_node(
    domain: Domain, model, workspace_service=None, collect: bool = False
):
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

    [claude] `collect` changes what the node writes, not what it does.

        False  append to `messages` — a single-domain graph, unchanged
        True   append to `findings`  — the supervisor graph, where two
               specialists may run at once

    Two nodes appending to `messages` in one step is what LangGraph raises
    `InvalidUpdateError` for, and the alternative — a last-write-wins field —
    would silently discard one specialist's work, which looks exactly like a
    complete answer. `findings` has a reducer for that reason.
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
        except GraphRecursionError:  # noqa: B902
            # [claude] The step ceiling was set but never caught, so hitting
            # it killed the whole run with an unhandled exception rather than
            # an answer. Degrade to a plain message instead: the loop is a bug
            # worth fixing wherever it comes from, but it should not take the
            # request down with it.
            ran_out = (
                "I wasn't able to complete that request — I ran out of "
                "steps while working on it. Try asking for something more "
                "specific."
            )

            if collect:
                return {
                    "findings": [
                        {"specialist": domain.name, "answer": ran_out}
                    ]
                }

            return {
                "messages": [AIMessage(content=ran_out)],
                "route": domain.name,
            }

        if collect:
            produced = result["messages"][len(state["messages"]) :]
            answer = produced[-1].content if produced else ""

            # [claude] The answer goes to `findings`; the work goes to
            # `messages`.
            #
            # Collect mode first returned findings alone, and everything
            # downstream that reads the trace went blank — `tools_used` in
            # the API response, the tool pills in the UI, and every
            # tool-call assertion in the complex suite, which dropped from
            # 12/12 to 0/12. The answer was fine; the record of how it was
            # reached had vanished.
            #
            # The final message is held back so `synthesise` provides the
            # one visible answer — otherwise a single-specialist turn ends
            # with the same text twice. `add_messages` is a real reducer, so
            # two specialists appending at once merge rather than collide.
            return {
                "findings": [
                    {"specialist": domain.name, "answer": str(answer or "")}
                ],
                "messages": list(produced[:-1]),
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
    [claude] The routed graph, now able to run two specialists and merge them.

        START
          |
        supervisor            one planning call, no tools
          |                   picks one route, or two
          +-- deals ------> deals_agent      --+
          +-- leads ------> leads_agent       |
          +-- workspace --> workspace_agent   +--> synthesise --> END
          +-- research ---> research_agent    |
          +-- general ----> general_agent   --+

    **The specialists are still sealed.** Each keeps its own guard and its
    own table set; orchestration decides *who runs*, never *what they can
    reach*. That is the part of design decision 3 worth keeping — a
    misrouted question still cannot be rescued by an agent reaching into
    another domain's data, because there is no path for it to.

    What changed is that a question with two halves gets both answered. "How
    do our cancellation rates compare with the market" is a deals question
    and a research question; routing it to either alone returns something
    that looks complete and silently drops half the question, which is the
    failure shape this project keeps finding.

    Specialists run in parallel and write to `findings`, which has a reducer
    for exactly that reason. `synthesise` merges them — and when the plan
    held one specialist it passes the answer straight through, so the common
    case costs precisely what it did before.
    """

    model = get_model()

    graph = StateGraph(AgentState)

    async def supervisor_node(state: AgentState):
        """Plan the turn. Adds no message — it only decides who runs."""

        plan = await choose_plan(model, state["messages"])

        # [claude] `findings: None` clears the previous turn's results.
        #
        # Without it they accumulate — `findings` is checkpointed and its
        # reducer concatenates — so the second turn in a conversation saw
        # the first turn's findings, believed two specialists had run, and
        # merged a stale answer into the new one. "okay" came back as a
        # list of property developers from the question before it.
        #
        # `route` keeps holding the primary specialist, unchanged, so every
        # existing trace, eval and API response keeps working.
        return {"plan": plan, "route": plan[0], "findings": None}

    def _toolless_node(name: str, prompt: str, tools=None):
        """
        [claude] A specialist with no CRM access, as a graph node.

        `general` and `research` are not Domains and must not become ones:
        a Domain binds a table set to a guard, and building one here would
        hand a `sql_query` tool to the two agents most exposed to being
        talked into something — one takes arbitrary user chat, the other
        reads pages written by strangers.
        """

        agent = (
            create_agent(model=model, tools=list(tools), system_prompt=prompt)
            if tools
            else None
        )

        async def node(state: AgentState):
            try:
                if agent is None:
                    reply = await model.ainvoke(
                        [SystemMessage(content=prompt), *state["messages"]]
                    )
                    answer = reply.content
                else:
                    result = await agent.ainvoke(
                        {"messages": state["messages"]},
                        config={"recursion_limit": MAX_AGENT_STEPS},
                    )
                    answer = result["messages"][-1].content
            except Exception:
                # A failure in one specialist must not take the turn down —
                # synthesise reports what it has.
                logger.exception("specialist_failed", extra={"specialist": name})
                answer = ""

            return {"findings": [{"specialist": name, "answer": str(answer or "")}]}

        return node

    graph.add_node("supervisor", supervisor_node)
    graph.add_node(
        GENERAL_ROUTE, _toolless_node(GENERAL_ROUTE, GENERAL_AGENT_SYSTEM_PROMPT)
    )
    graph.add_node(
        RESEARCH_ROUTE,
        _toolless_node(
            RESEARCH_ROUTE, RESEARCH_AGENT_SYSTEM_PROMPT, tools=build_web_tools()
        ),
    )

    for name, domain in DOMAINS.items():
        graph.add_node(
            f"{name}_agent", make_domain_node(domain, model, collect=True)
        )

    async def synthesise(state: AgentState):
        """
        Turn what the specialists found into one answer.

        [claude] One specialist is passed through verbatim rather than
        rewritten. A merge step would paraphrase a number that was verified
        against SQL, and paraphrasing a figure is how "closing soonest"
        became "earliest closing dates" — the answer is already correct, so
        the cheapest and safest thing is to leave it alone.

        Two specialists are merged by one model call, told explicitly not to
        alter figures.
        """

        findings = sorted(
            state.get("findings") or [], key=lambda f: f["specialist"]
        )
        answered = [f for f in findings if f["answer"].strip()]

        if not answered:
            return {"messages": [out_of_scope_message()]}

        if len(answered) == 1:
            return {"messages": [AIMessage(content=answered[0]["answer"])]}

        parts = "\n\n".join(
            f"--- from the {f['specialist']} specialist ---\n{f['answer']}"
            for f in answered
        )

        try:
            merged = await model.ainvoke(
                [
                    SystemMessage(content=SYNTHESIS_PROMPT),
                    *state["messages"],
                    SystemMessage(content=parts),
                ]
            )
            return {"messages": [merged]}
        except Exception:
            logger.exception("synthesis_failed")

            # Better two labelled answers than none.
            return {"messages": [AIMessage(content=parts)]}

    graph.add_node("synthesise", synthesise)

    graph.set_entry_point("supervisor")

    def _fan_out(state: AgentState):
        """Every specialist the plan named, run together."""

        plan = state.get("plan") or [state.get("route", FALLBACK_ROUTE)]

        return [
            name if name in _TOOLLESS else f"{name}_agent"
            for name in plan
        ]

    graph.add_conditional_edges(
        "supervisor",
        _fan_out,
        [*(f"{name}_agent" for name in DOMAINS), *_TOOLLESS],
    )

    for name in DOMAINS:
        graph.add_edge(f"{name}_agent", "synthesise")

    for name in _TOOLLESS:
        graph.add_edge(name, "synthesise")

    graph.set_finish_point("synthesise")

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
