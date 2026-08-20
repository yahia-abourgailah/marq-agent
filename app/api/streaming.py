"""
[claude] Turning a graph run into a stream of SSE events.

What the front end gets, and why the filtering below is not cosmetic
--------------------------------------------------------------------
A turn here is two to three model calls and can take many seconds, so a chat
UI needs something to show before the answer exists. But most of the tokens
this graph generates must never be shown, and that was measured rather than
assumed. Streaming the raw event feed of one question — "How many deals are
there in total?" — produces tokens from two sources the user must not see:

    node='supervisor'   ->  'deals'
    node='model'        ->  'SELECT count(*) AS deals_count FROM deals ...'

The first is the routing decision. The second is the SQL Agent writing the
query, and it shares the node name `model` with the domain agent's final
answer — so filtering by node alone shows the user generated SQL.

What separates them is the tool boundary: the SQL Agent runs *inside* the
`sql_query` tool, between `on_tool_start` and `on_tool_end`. So the rule is:

    emit a token only when we are not inside a tool, and not in the supervisor

Verified end to end against the live graph before this was written. With the
filter, that same question emits exactly `'There are 315 deals in total.'` and
suppresses `'deals'` plus the full SELECT.

Event types
-----------
    route     the classifier's decision, once
    tool      a tool started; gives the UI something honest to show
    token     a fragment of the answer
    final     the complete answer, plus route and tools used
    error     a typed failure
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import HumanMessage

from app.auth.principal import Principal
from app.config import settings
from app.sql import provenance

logger = logging.getLogger("marq.api")

# The node that classifies the turn. Its tokens are the route name.
SUPERVISOR_NODE = "supervisor"


def sse(event: str, data: dict[str, Any]) -> dict[str, str]:
    """One Server-Sent Event, as sse-starlette wants it."""

    return {"event": event, "data": json.dumps(data, default=str)}


def route_from(output: Any) -> str | None:
    """
    The route out of a supervisor `on_chain_end` payload.

    [claude] Written against the real event stream after the first version
    crashed on it. Two `on_chain_end` events carry
    `langgraph_node == "supervisor"`, and their outputs are different shapes:

        name='Unnamed'      output = 'deals'              <- the bare string
        name='supervisor'   output = {'route': 'deals'}   <- the node's return

    The first version assumed the dict and called `.get()` on a `str`, which
    raised `AttributeError` and turned every streamed turn into an error
    event. The hermetic test did not catch it because the stub emitted the
    shape I believed in rather than the shape that exists — the same trap
    docs/HANDOFF.md records for `owner_name_joins_users`, where an existing
    test was evidence about a belief and not about the world.

    So this accepts either, and anything else returns None rather than
    raising: a stream must not die because a future LangGraph version adds a
    third shape.
    """

    if isinstance(output, str):
        return output.strip() or None

    if isinstance(output, dict):
        value = output.get("route")

        return value if isinstance(value, str) and value.strip() else None

    return None


def answer_of(messages: list[Any]) -> str:
    """
    The reply to show, taken from the last AI message with text in it.

    [claude] Not simply `messages[-1]`. A run that ends on a tool call or a
    tool result would render as an empty answer, which reads to the user as
    the agent having nothing to say rather than as a bug.
    """

    for message in reversed(messages):
        content = getattr(message, "content", None)

        if isinstance(content, str) and content.strip():
            if getattr(message, "type", None) in ("ai", "AIMessageChunk"):
                return content.strip()

    return ""


def tools_used_in(messages: list[Any]) -> list[str]:
    """Every tool the agent called, in order. Shown in the trace."""

    return [
        call["name"]
        for message in messages
        for call in (getattr(message, "tool_calls", None) or [])
    ]


def provenance_of(collector: provenance.Collector | None) -> list[dict[str, Any]]:
    """
    The queries behind a turn, as JSON.

    [claude] Empty when provenance is switched off, and empty for a turn
    that ran no queries — an out-of-scope reply, or one answered entirely
    from an uploaded file. Both are honest: the field says what the database
    was asked, and sometimes the answer is "nothing".
    """

    if collector is None or not settings.expose_provenance:
        return []

    return [record.as_dict() for record in collector.records]


def graph_input(principal: Principal, message: str) -> dict[str, Any]:
    """
    The graph's input state for one turn.

    [claude] The single place identity enters the graph. Both ids come off
    the verified `Principal` and neither is reachable from the request body —
    see app/api/schemas.py, where the request models forbid them outright.
    """

    return {
        "messages": [HumanMessage(content=message)],
        "requester_id": principal.requester_id,
        "workspace_id": principal.workspace_id,
    }


def run_config(thread_key: str, request_id: str | None = None) -> dict[str, Any]:
    """
    LangGraph config for one turn.

    `thread_key` is already namespaced to the caller by
    `Principal.thread_key()`, so the checkpointer cannot be addressed with a
    raw client-supplied id.
    """

    return {
        "configurable": {"thread_id": thread_key},
        "metadata": {"request_id": request_id},
    }


async def stream_turn(
    graph: Any,
    principal: Principal,
    message: str,
    thread_key: str,
    thread_id: str,
    request_id: str | None = None,
) -> AsyncIterator[dict[str, str]]:
    """
    Run one turn, yielding SSE events as it goes.

    Ends with a `final` event carrying the whole answer, so a client can
    ignore `token` entirely and still be correct — useful for non-browser
    callers and for reconnects.
    """

    yield sse("start", {"thread_id": thread_id})

    tool_depth = 0
    route: str | None = None
    tools: list[str] = []
    parts: list[str] = []
    plan: list[str] = []

    # [claude] Collects the SQL each tool call runs, out of band — the
    # queries never enter the model's context. See app/sql/provenance.py.
    with provenance.collect() as collector:
        try:
            async for event in graph.astream_events(
                graph_input(principal, message),
                config=run_config(thread_key, request_id),
                version="v2",
            ):
                kind = event["event"]
                node = (event.get("metadata") or {}).get("langgraph_node")

                if kind == "on_tool_start":
                    tool_depth += 1

                    name = event.get("name", "")
                    tools.append(name)

                    yield sse("tool", {"name": name})

                elif kind == "on_tool_end":
                    # Clamped at zero: a malformed pairing must not leave
                    # the counter negative, which would un-suppress the SQL
                    # agent's tokens.
                    tool_depth = max(0, tool_depth - 1)

                elif kind == "on_chat_model_stream":
                    if node == SUPERVISOR_NODE or tool_depth > 0:
                        # The route decision, or the SQL agent thinking.
                        continue

                    text = getattr(event["data"].get("chunk"), "content", "")

                    if text:
                        parts.append(text)

                        yield sse("token", {"text": text})

                elif kind == "on_chain_end" and node == SUPERVISOR_NODE:
                    output = event.get("data", {}).get("output")

                    # [claude] The plan arrives with the route and is worth
                    # showing: a two-specialist turn takes noticeably longer,
                    # and a UI that says why is better than one that appears
                    # to hang.
                    if isinstance(output, dict) and output.get("plan"):
                        planned = [str(p) for p in output["plan"]]

                        if planned != plan:
                            plan = planned
                            yield sse("plan", {"specialists": plan})

                    decided = route_from(output)

                    if decided and decided != route:
                        route = decided

                        yield sse("route", {"route": route})

        except Exception:
            # [claude] Logged in full, reported as one sentence — the same
            # rule as app/api/errors.py. A stream cannot change its status
            # code once it has started, so the error has to arrive as an
            # event.
            logger.exception(
                "stream_failed",
                extra={"request_id": request_id, "thread_id": thread_id},
            )

            yield sse(
                "error",
                {
                    "code": "internal_error",
                    "message": "Something went wrong answering that question.",
                    "request_id": request_id,
                },
            )

            return

        yield sse(
            "final",
            {
                "thread_id": thread_id,
                "answer": "".join(parts).strip(),
                "route": route,
                "specialists": plan,
                "tools_used": tools,
                "provenance": provenance_of(collector),
            },
        )


__all__ = [
    "SUPERVISOR_NODE",
    "answer_of",
    "provenance_of",
    "route_from",
    "graph_input",
    "run_config",
    "sse",
    "stream_turn",
    "tools_used_in",
]
