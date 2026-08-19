"""
[claude] Recording the SQL behind an answer.

Why this exists
---------------
The agent says "There are 315 deals in total" and the user has no way to
check it. Every bug this project has found was a *confident wrong number*
rather than an exception — and every one was found by someone running SQL by
hand. docs/HANDOFF.md's first working-practice note is "verify answers
against SQL, not just that nothing crashed", and until now only a developer
with a psql prompt could do that.

So the query is captured and returned alongside the answer, which turns an
unfalsifiable claim into a checkable one.

Why a context-local collector rather than the tool payload
----------------------------------------------------------
The obvious alternative is adding `"sql"` to what `sql_query` returns. That
was rejected for two reasons:

*   It enters the model's context. `app/tools/sql.py` already caps its
    payload because row *width* is what blows the context window, and adding
    a query string to every tool result spends tokens on something the agent
    does not need — it wrote the query, it is not going to read it back.

*   It invites the agent to quote SQL at the user. The prompts work hard to
    keep answers in business language.

A ContextVar keeps the record entirely out of band: the tool writes to it,
the HTTP layer reads it, and nothing in between sees it.

The mechanism relies on `asyncio.Task` copying the current context at
creation. The collector is *mutated*, never rebound, so appends made inside
LangGraph's own tasks are visible to the request that started them. A run
with no collector set records nothing at all, which is what keeps the CLI,
the tests and the eval suites unaffected.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

# None means "nobody is collecting" — the default everywhere except inside an
# HTTP request that asked for provenance.
_COLLECTOR: ContextVar[list[QueryRecord] | None] = ContextVar(
    "sql_provenance", default=None
)


@dataclass(frozen=True)
class QueryRecord:
    """One database question, and what came back."""

    sql: str | None

    # What the agent asked for, in words. Kept because a turn can run several
    # queries and the SQL alone does not say which part of the answer it
    # supports.
    question: str | None = None

    rows_available: int | None = None
    truncated: bool = False

    # Set instead of `sql` when the SQL agent declined — a refusal is a
    # typed outcome here, not a failure, and it is worth showing: "no query
    # was run, and here is why" is a more useful answer than silence.
    refused: str | None = None

    def as_dict(self) -> dict:
        return {
            "sql": self.sql,
            "question": self.question,
            "rows_available": self.rows_available,
            "truncated": self.truncated,
            "refused": self.refused,
        }


@dataclass
class Collector:
    """The records gathered during one turn."""

    records: list[QueryRecord] = field(default_factory=list)


@contextmanager
def collect():
    """
    Collect every query run inside this block.

    Yields the `Collector`. Restores whatever was previously in place on the
    way out, so nesting cannot strand a collector and leak one request's
    queries into the next.
    """

    collector = Collector()
    token = _COLLECTOR.set(collector.records)

    try:
        yield collector
    finally:
        _COLLECTOR.reset(token)


def record(
    sql: str | None = None,
    question: str | None = None,
    rows_available: int | None = None,
    truncated: bool = False,
    refused: str | None = None,
) -> None:
    """
    Note one query, if anything is collecting.

    A no-op otherwise, deliberately: the SQL tool calls this unconditionally
    and must not care whether it is running under HTTP, the CLI or an eval.
    """

    records = _COLLECTOR.get()

    if records is None:
        return

    records.append(
        QueryRecord(
            sql=sql,
            question=question,
            rows_available=rows_available,
            truncated=truncated,
            refused=refused,
        )
    )


__all__ = ["Collector", "QueryRecord", "collect", "record"]
