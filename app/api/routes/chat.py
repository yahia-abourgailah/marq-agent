"""
[claude] The chat endpoints — the front end's main entry point.

Two paths to the same turn:

    POST /chat          runs to completion, returns one JSON body
    POST /chat/stream   the same turn as Server-Sent Events

Both are kept because they fail differently. A browser wants tokens as they
arrive; a script, a test, or another service wants one response it can assert
on. Sharing `graph_input` and `run_config` between them means the identity
plumbing cannot drift apart, which is the part that matters.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.api.deps import Conversations, CurrentPrincipal, Graph
from app.api.errors import request_id_of
from app.api.schemas import ChatRequest, ChatResponse
from app.api.streaming import (
    answer_of,
    charts_of,
    graph_input,
    provenance_of,
    run_config,
    stream_turn,
)
from app.db.repositories.conversations import make_title
from app.graph.state import COMPLETED, ERROR
from app.sql import provenance
from app.tools import charts as chart_tools

logger = logging.getLogger("marq.api")

router = APIRouter(tags=["chat"])


def _thread_id(requested: str | None) -> str:
    """Continue a conversation, or start one."""

    return requested or uuid.uuid4().hex


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    request: Request,
    principal: CurrentPrincipal,
    conversations: Conversations,
    graph: Graph,
) -> ChatResponse:
    """
    Ask one question and wait for the whole answer.

    Note what is *not* in `body`: the workspace and the requester. Both are
    taken from the verified token below, and `ChatRequest` forbids them as
    fields so sending one is a 422 rather than a silently ignored value.
    """

    thread_id = _thread_id(body.thread_id)
    thread_key = principal.thread_key(thread_id)

    logger.info(
        "chat_turn",
        extra={
            "request_id": request_id_of(request),
            "subject": principal.subject,
            "thread_id": thread_id,
        },
    )

    # [claude] Collects the SQL behind the answer without it ever entering
    # the model's context — see app/sql/provenance.py. The same helper wraps
    # the streaming path, so the two cannot report different queries for the
    # same turn.
    with provenance.collect() as collector, chart_tools.collect() as drawn:
        try:
            result = await graph.ainvoke(
                graph_input(principal, body.message),
                config=run_config(thread_key, request_id_of(request)),
            )
        except Exception:
            # [claude] Re-raised immediately — the error handlers own the
            # response, and this changes nothing a caller sees. It exists so
            # that a turn which crashed is countable in the same field as a
            # turn that ran out of steps. A monitor that has to join two
            # differently-named events to count failures will not.
            logger.info(
                "chat_turn_complete",
                extra={
                    "request_id": request_id_of(request),
                    "subject": principal.subject,
                    "thread_id": thread_id,
                    "stop_reason": ERROR,
                    "streamed": False,
                },
            )
            raise

    messages = result["messages"]

    # [claude] The reason the turn ended, beside the fact that it ended.
    #
    # `chat_turn` above is emitted before the graph runs, so on its own it
    # says only that a question arrived. This is the line that says what
    # happened to it — and until it existed, an out-of-steps run was
    # invisible: the agent degrades to an apology in prose, the endpoint
    # returns 200, and nothing anywhere recorded that the ceiling was hit.
    #
    # Defaults to `completed` only for a graph that published no reason at
    # all; every graph built here publishes one.
    stop_reason = result.get("stop_reason") or COMPLETED

    logger.info(
        "chat_turn_complete",
        extra={
            "request_id": request_id_of(request),
            "subject": principal.subject,
            "thread_id": thread_id,
            "route": result.get("route"),
            "stop_reason": stop_reason,
            "streamed": False,
        },
    )

    # [claude] Recorded after the answer, not before. A question that raised
    # leaves no conversation in the user's list, so the list never contains a
    # thread with nothing in it.
    records = provenance_of(collector)

    await conversations.record_turn(
        subject=principal.subject,
        thread_id=thread_id,
        thread_key=thread_key,
        title=make_title(body.message),
        provenance=records,
    )

    return ChatResponse(
        thread_id=thread_id,
        answer=answer_of(messages),
        route=result.get("route"),
        specialists=list(result.get("plan") or []),
        # [claude] From `trace`, not from the transcript. Tool payloads
        # no longer travel in `messages` — see state.py — and the names are
        # all this ever needed.
        tools_used=list(result.get("trace") or []),
        provenance=records,
        charts=charts_of(drawn),
        stop_reason=stop_reason,
    )


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    principal: CurrentPrincipal,
    conversations: Conversations,
    graph: Graph,
) -> EventSourceResponse:
    """
    The same turn, streamed.

    The generator suppresses the routing tokens and everything the SQL Agent
    generates — see app/api/streaming.py, where the filter is derived from a
    measured run rather than assumed.
    """

    thread_id = _thread_id(body.thread_id)
    thread_key = principal.thread_key(thread_id)

    logger.info(
        "chat_turn_stream",
        extra={
            "request_id": request_id_of(request),
            "subject": principal.subject,
            "thread_id": thread_id,
        },
    )

    async def events():
        answered = False
        records: list = []

        async for event in stream_turn(
            graph=graph,
            principal=principal,
            message=body.message,
            thread_key=thread_key,
            thread_id=thread_id,
            request_id=request_id_of(request),
        ):
            if event["event"] == "final":
                answered = True

                # [claude] Read back off the event rather than reaching for
                # the collector, whose context has already been left by the
                # time this generator resumes.
                records = json.loads(event["data"]).get("provenance", [])

            yield event

        if answered:
            await conversations.record_turn(
                subject=principal.subject,
                thread_id=thread_id,
                thread_key=thread_key,
                title=make_title(body.message),
                provenance=records,
            )

    return EventSourceResponse(events())


__all__ = ["router"]
