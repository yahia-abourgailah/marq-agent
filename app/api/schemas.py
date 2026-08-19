"""
[claude] Request and response bodies for the HTTP API.

One rule runs through all of them: **identity is never a field.**

`requester_id` and `workspace_id` reach the graph from the verified token and
from nowhere else. docs/HANDOFF.md states the reasoning for the tool layer —
"a model that could name its own workspace could name someone else's" — and it
is sharper here, because the caller is a browser rather than a model.

That is why the request models set `extra="forbid"`. A front end that sends
`workspace_id` gets a 422 telling it the field is not accepted, instead of a
200 whose answer quietly came from the caller's own workspace anyway. Silently
ignoring an unexpected field is how someone concludes it worked.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    """One turn of a conversation."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=1,
        max_length=4000,
        description="The user's question.",
    )

    thread_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
        description=(
            "Conversation to continue. Omit to start a new one. Scoped to "
            "the authenticated caller, so two users may use the same id "
            "without ever seeing each other's conversation."
        ),
    )


class ChatResponse(BaseModel):
    """A completed turn."""

    thread_id: str
    answer: str

    route: str | None = Field(
        default=None,
        description=(
            "Which domain answered: deals, leads, workspace or out_of_scope. "
            "Exposed because a wrong answer is often a routing mistake, and "
            "the front end showing it makes that visible rather than "
            "mysterious."
        ),
    )

    tools_used: list[str] = Field(default_factory=list)


class ConversationSummary(BaseModel):
    """One row of the conversation list."""

    thread_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    turn_count: int


class ConversationList(BaseModel):
    conversations: list[ConversationSummary]


class Message(BaseModel):
    """One message in a replayed conversation."""

    role: str = Field(description="user, assistant, or tool")
    content: str


class ConversationDetail(BaseModel):
    thread_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    turn_count: int
    messages: list[Message]


class UploadedSheet(BaseModel):
    name: str
    row_count: int
    columns: list[str]


class UploadedFile(BaseModel):
    """A file in the caller's workspace."""

    file_id: str
    filename: str
    kind: str
    byte_size: int
    sheets: list[UploadedSheet] = Field(default_factory=list)
    page_count: int | None = None


class UploadResponse(BaseModel):
    file: UploadedFile
    chunks_indexed: int

    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal problems — an unreachable vector index, a PDF with no "
            "extractable text. Surfaced rather than swallowed: a file that "
            "parsed but was not indexed is searchable-looking and not "
            "searchable."
        ),
    )


class FileList(BaseModel):
    files: list[UploadedFile]


class DeleteResponse(BaseModel):
    deleted: bool


class ComponentHealth(BaseModel):
    ok: bool
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    """
    What is actually reachable.

    [claude] Reports what each dependency *can do*, not what it is
    configured as. docs/HANDOFF.md records the read-only role that existed,
    looked configured, and could read nothing — the two came apart, and a
    check reading configuration would have called it healthy.
    """

    ready: bool
    database: ComponentHealth
    checkpointer: ComponentHealth
    model: ComponentHealth
    vector_index: ComponentHealth


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ComponentHealth",
    "ConversationDetail",
    "ConversationList",
    "ConversationSummary",
    "DeleteResponse",
    "FileList",
    "HealthResponse",
    "Message",
    "ReadinessResponse",
    "UploadResponse",
    "UploadedFile",
    "UploadedSheet",
]
