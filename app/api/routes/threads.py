"""
[claude] Conversation history.

Every route here is scoped by the authenticated subject, and the scoping is in
the SQL rather than in a check afterwards — see
`ConversationRepository.get()`. A thread belonging to another employee is
reported as absent, not as forbidden: a 403 that differs from a 404 tells a
caller which thread ids exist, which is a slow enumeration of everyone's
conversations.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request, status

from app.api.deps import Conversations, CurrentPrincipal
from app.api.errors import ApiError
from app.api.schemas import (
    ConversationDetail,
    ConversationList,
    ConversationSummary,
    DeleteResponse,
    Message,
)

logger = logging.getLogger("marq.api")

router = APIRouter(prefix="/threads", tags=["threads"])

# LangChain message types mapped to what a front end wants to render.
_ROLES = {
    "human": "user",
    "ai": "assistant",
    "tool": "tool",
    "system": "system",
}


@router.get("", response_model=ConversationList)
async def list_threads(
    principal: CurrentPrincipal,
    conversations: Conversations,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ConversationList:
    """This caller's conversations, most recently updated first."""

    rows = await conversations.list_for(
        principal.subject, limit=limit, offset=offset
    )

    return ConversationList(
        conversations=[
            ConversationSummary(
                thread_id=row.thread_id,
                title=row.title,
                created_at=row.created_at,
                updated_at=row.updated_at,
                turn_count=row.turn_count,
            )
            for row in rows
        ]
    )


@router.get("/{thread_id}", response_model=ConversationDetail)
async def get_thread(
    thread_id: str,
    request: Request,
    principal: CurrentPrincipal,
    conversations: Conversations,
) -> ConversationDetail:
    """
    Replay one conversation.

    Two stores are consulted, because they hold different halves: the index
    row proves ownership and carries the metadata, and the checkpointer holds
    the messages. Ownership is settled *first* — the checkpointer is only
    addressed with a thread key derived from a row this subject owns.
    """

    row = await conversations.get(principal.subject, thread_id)

    if row is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            "No such conversation.",
        )

    saver = request.app.state.checkpointer.saver

    snapshot = await saver.aget_tuple(
        {"configurable": {"thread_id": principal.thread_key(thread_id)}}
    )

    messages = []

    if snapshot is not None:
        for message in snapshot.checkpoint.get("channel_values", {}).get(
            "messages", []
        ):
            content = getattr(message, "content", "")

            # [claude] Tool messages are dropped rather than rendered. They
            # are raw query results — CRM rows — and a history view is not
            # the place to re-serve them. The answer that quoted them is
            # already in the transcript.
            role = _ROLES.get(getattr(message, "type", ""), "assistant")

            if role == "tool" or not isinstance(content, str) or not content:
                continue

            messages.append(Message(role=role, content=content))

    return ConversationDetail(
        thread_id=row.thread_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        turn_count=row.turn_count,
        messages=messages,
    )


@router.delete("/{thread_id}", response_model=DeleteResponse)
async def delete_thread(
    thread_id: str,
    request: Request,
    principal: CurrentPrincipal,
    conversations: Conversations,
) -> DeleteResponse:
    """
    Forget one conversation, messages and all.

    [claude] Order matters: checkpoints first, index row last.

    Deleting the index row first would leave the messages in the checkpointer
    with nothing pointing at them — unlistable, undeletable through this API,
    and still holding CRM answers. Doing it this way round, a failure halfway
    leaves a conversation that is still listed and can be deleted again,
    which is the recoverable direction.
    """

    row = await conversations.get(principal.subject, thread_id)

    if row is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            "No such conversation.",
        )

    saver = request.app.state.checkpointer.saver
    thread_key = principal.thread_key(thread_id)

    delete_thread_fn = getattr(saver, "adelete_thread", None)

    if delete_thread_fn is not None:
        await delete_thread_fn(thread_key)

    deleted = await conversations.delete(principal.subject, thread_id)

    logger.info(
        "thread_deleted",
        extra={"subject": principal.subject, "thread_id": thread_id},
    )

    return DeleteResponse(deleted=deleted)


__all__ = ["router"]
