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
import re
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import HumanMessage

from app.auth.principal import Principal
from app.config import settings
from app.graph.state import COMPLETED, ERROR, STOP_REASONS
from app.sql import provenance
from app.tools import charts as chart_tools

logger = logging.getLogger("marq.api")

# The node that classifies the turn. Its tokens are the route name.
SUPERVISOR_NODE = "supervisor"


def sse(event: str, data: dict[str, Any]) -> dict[str, str]:
    """One Server-Sent Event, as sse-starlette wants it."""

    return {"event": event, "data": json.dumps(data, default=str)}


# [claude] Tool-call syntax that leaked into prose.
#
# When a model wants a tool it does not have, it sometimes emits the call
# as *text* rather than as a structured call. The user then sees
# `<|tool_call>call:make_chart{kind:<|"|>pie<|"|>…}` where an answer should
# be — gibberish, and gibberish that looks like the system broke open.
#
# The real fix is giving the agent the tool it reached for, which is done.
# This is the guard for the next time, because a leak is unreadable
# whatever caused it, and the failure is silent: nothing raises, the turn
# "succeeds", and only a person reading the screen can tell.
_TOOL_CALL_LEAK = re.compile(
    r"<\|?\s*tool_call.*?tool_call\s*\|?>"      # <|tool_call>…<tool_call|>
    r"|<tool_call>.*?</tool_call>"                # <tool_call>…</tool_call>
    r"|\{\s*\"?name\"?\s*:\s*\"?(?:make_chart|sql_query|web_search)\b.*?\}",
    re.DOTALL | re.IGNORECASE,
)

# The per-token escape some servers wrap string arguments in. Left behind
# when the block above matches only part of a malformed call.
_TOKEN_ARTEFACT = re.compile(r"<\|\"\|>")


def strip_tool_leak(text: str) -> str:
    """Remove tool-call syntax that reached the prose."""

    cleaned = _TOOL_CALL_LEAK.sub("", text or "")
    cleaned = _TOKEN_ARTEFACT.sub("", cleaned)

    return cleaned.strip()


def clean_answer(text: str, request_id: str | None = None) -> str:
    """
    The answer as a person should see it.

    Returns a plain apology rather than an empty bubble when a reply was
    *entirely* a leaked call — an empty answer reads as the agent ignoring
    the question.
    """

    original = text or ""
    cleaned = strip_tool_leak(original)

    if cleaned == original.strip():
        return original.strip()

    logger.warning(
        "tool_call_leaked_into_answer",
        extra={"request_id": request_id, "removed": len(original) - len(cleaned)},
    )

    if not cleaned:
        return (
            "I wasn't able to put that together properly — please ask again."
        )

    return cleaned


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


def stop_reason_from(output: Any) -> str | None:
    """
    A turn's stop reason out of an `on_chain_end` payload, if it carries one.

    [claude] Applied to *every* chain-end rather than to a named node, and
    that is deliberate. Matching on `langgraph_node == "synthesise"` would
    tie the streaming path to one graph's node names — and `route_from`
    above is already a written record of what assuming an event shape costs
    here. Any node that publishes a valid reason is believed; the last one
    wins, which is the node nearest the end of the run.

    Only the four known values are accepted. A future node writing
    something else into the channel should read as "no reason observed"
    rather than reach the front end as a status nobody defined.
    """

    if not isinstance(output, dict):
        return None

    value = output.get("stop_reason")

    return value if value in STOP_REASONS else None


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
                return clean_answer(content)

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


def charts_of(collector) -> list[dict[str, Any]]:
    """The charts drawn during a turn, as JSON."""

    return list(collector.charts) if collector is not None else []


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
    stop_reason: str | None = None

    # [claude] Collects the SQL each tool call runs, out of band — the
    # queries never enter the model's context. See app/sql/provenance.py.
    with provenance.collect() as collector, chart_tools.collect() as drawn:
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

                elif kind == "on_chain_end":
                    output = event.get("data", {}).get("output")

                    # [claude] Checked on every chain-end, not just the
                    # supervisor's — see stop_reason_from. The supervisor
                    # clears the field at the top of the turn and the
                    # reader ignores that, so the value that survives is
                    # the one the final node published.
                    observed = stop_reason_from(output)

                    if observed is not None:
                        stop_reason = observed

                    if node != SUPERVISOR_NODE:
                        continue

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
                extra={
                    "request_id": request_id,
                    "thread_id": thread_id,
                    "stop_reason": ERROR,
                },
            )

            # [claude] Logged as a completed turn too, with `error` as the
            # reason. A monitor counting stop_reason should be able to see
            # every turn that started, and a turn that died mid-stream is
            # the one it most needs to count.
            logger.info(
                "chat_turn_complete",
                extra={
                    "request_id": request_id,
                    "subject": principal.subject,
                    "thread_id": thread_id,
                    "route": route,
                    "stop_reason": ERROR,
                    "streamed": True,
                },
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

        # [claude] Defaulted only here, at the point the run has actually
        # finished without raising. Defaulting earlier would mean an
        # unobserved reason reads as `completed`, which is the exact
        # confusion this field exists to remove.
        stop_reason = stop_reason or COMPLETED

        logger.info(
            "chat_turn_complete",
            extra={
                "request_id": request_id,
                "subject": principal.subject,
                "thread_id": thread_id,
                "route": route,
                "stop_reason": stop_reason,
                "streamed": True,
            },
        )

        yield sse(
            "final",
            {
                "thread_id": thread_id,
                "answer": clean_answer("".join(parts), request_id),
                "route": route,
                "specialists": plan,
                "tools_used": tools,
                "provenance": provenance_of(collector),
                "charts": charts_of(drawn),
                "stop_reason": stop_reason,
            },
        )


__all__ = [
    "SUPERVISOR_NODE",
    "answer_of",
    "clean_answer",
    "strip_tool_leak",
    "charts_of",
    "provenance_of",
    "route_from",
    "stop_reason_from",  # [claude]
    "graph_input",
    "run_config",
    "sse",
    "stream_turn",
    "tools_used_in",
]
