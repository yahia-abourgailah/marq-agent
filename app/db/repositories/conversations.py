"""
[claude] The conversation index — which threads belong to which employee.

Companion to the LangGraph checkpointer rather than a replacement for it. The
checkpointer holds the messages; this holds ownership and enough metadata to
list a conversation without loading one. See migrations/003_conversations.sql
for why the checkpointer cannot answer either question on its own.

Every method takes `subject` first and scopes its statement by it. There is no
method that omits it — the same rule `WorkspaceService` follows for
`workspace_id`, and for the same reason: a method that could be called
without the scope is a method that eventually is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

# A title is a display label cut from the first question, not content. Long
# enough to tell two conversations apart, short enough not to become a second
# copy of customer data in a second table.
TITLE_MAX_CHARS = 120


@dataclass(frozen=True)
class Conversation:
    """One conversation, as the front end sees it."""

    thread_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    turn_count: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Conversation:
        return cls(
            thread_id=row["thread_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            turn_count=row["turn_count"],
        )


def make_title(question: str) -> str:
    """Cut a display label from the first question of a conversation."""

    collapsed = " ".join(question.split())

    if len(collapsed) <= TITLE_MAX_CHARS:
        return collapsed

    return collapsed[: TITLE_MAX_CHARS - 1].rstrip() + "…"


class ConversationRepository:
    """Ownership and metadata for conversations, in the state database."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def record_turn(
        self,
        subject: str,
        thread_id: str,
        thread_key: str,
        title: str | None = None,
    ) -> None:
        """
        Note that a turn completed on this conversation.

        [claude] An upsert, so the first turn creates the row and every later
        one bumps the counter. Called *after* the agent answers rather than
        before, so a question that failed does not leave a conversation in
        the list that has nothing in it.

        The title is only written when the row is created — COALESCE keeps
        the original. A conversation is named after the question that started
        it, and renaming it on every turn would make the list reorder itself
        under the user as they typed.
        """

        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO conversations
                    (thread_key, subject, thread_id, title, turn_count)
                VALUES (%s, %s, %s, %s, 1)
                ON CONFLICT (thread_key) DO UPDATE
                SET turn_count = conversations.turn_count + 1,
                    updated_at = now(),
                    title      = COALESCE(conversations.title, EXCLUDED.title)
                """,
                (thread_key, subject, thread_id, title),
            )

    async def list_for(
        self,
        subject: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """This employee's conversations, most recently updated first."""

        async with self.pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT thread_id, title, created_at, updated_at, turn_count
                    FROM conversations
                    WHERE subject = %s
                    ORDER BY updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (subject, limit, offset),
                )

                return [
                    Conversation.from_row(row) for row in await cursor.fetchall()
                ]

    async def get(self, subject: str, thread_id: str) -> Conversation | None:
        """
        One conversation, or None if this employee does not have it.

        [claude] `subject` is in the WHERE clause, not checked after the
        fetch. Another employee's thread id is indistinguishable from one
        that does not exist, which is the correct answer to give — a 404 that
        differs from a 403 tells a caller which thread ids are real.
        """

        async with self.pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT thread_id, title, created_at, updated_at, turn_count
                    FROM conversations
                    WHERE subject = %s AND thread_id = %s
                    """,
                    (subject, thread_id),
                )

                row = await cursor.fetchone()

        return Conversation.from_row(row) if row else None

    async def delete(self, subject: str, thread_id: str) -> bool:
        """
        Forget one conversation. True if there was one to forget.

        Removes the index row. The checkpoints themselves are deleted by the
        route, which holds the saver — see app/api/routes/threads.py, and
        note the ordering there: checkpoints first, index last, so a failure
        leaves a listable conversation rather than an unreachable orphan.
        """

        async with self.pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM conversations WHERE subject = %s AND thread_id = %s",
                    (subject, thread_id),
                )

                return cursor.rowcount > 0


__all__ = [
    "TITLE_MAX_CHARS",
    "Conversation",
    "ConversationRepository",
    "make_title",
]
