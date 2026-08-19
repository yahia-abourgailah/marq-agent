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

import logging
import uuid

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.api.deps import Conversations, CurrentPrincipal, Graph
from app.api.errors import request_id_of
from app.api.schemas import ChatRequest, ChatResponse
from app.api.streaming import (
    answer_of,
    graph_input,
    run_config,
    stream_turn,
    tools_used_in,
)
from app.db.repositories.conversations import make_title

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

    result = await graph.ainvoke(
        graph_input(principal, body.message),
        config=run_config(thread_key, request_id_of(request)),
    )

    messages = result["messages"]

    # [claude] Recorded after the answer, not before. A question that raised
    # leaves no conversation in the user's list, so the list never contains a
    # thread with nothing in it.
    await conversations.record_turn(
        subject=principal.subject,
        thread_id=thread_id,
        thread_key=thread_key,
        title=make_title(body.message),
    )

    return ChatResponse(
        thread_id=thread_id,
        answer=answer_of(messages),
        route=result.get("route"),
        tools_used=tools_used_in(messages),
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

            yield event

        if answered:
            await conversations.record_turn(
                subject=principal.subject,
                thread_id=thread_id,
                thread_key=thread_key,
                title=make_title(body.message),
            )

    return EventSourceResponse(events())


__all__ = ["router"]
