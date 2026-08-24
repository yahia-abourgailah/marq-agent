"""
[claude] Builders and fakes for the HTTP API tests.

Kept out of conftest.py for the same reason as `workspace_support.py` — the
API test modules import these by name, so a reader can see where the fixtures
come from.

Two decisions worth knowing before reading the tests:

*   **The app is driven through ASGI directly, not through TestClient.**
    `httpx.ASGITransport` never runs the lifespan, and the lifespan opens
    PostgreSQL pools, builds a real model client and constructs the graph.
    Running it would make every test in this file an integration test. The
    pieces the lifespan would have built are injected onto `app.state`
    instead, which is also what lets one test swap the graph for a stub that
    records what it was asked.

*   **`create_app()` is the app under test.** Not a hand-rolled router
    assembly that happens to mount the same handlers. docs/HANDOFF.md is
    explicit that "a green suite over a path nobody uses is not evidence" —
    the middleware, the error handlers and the prefix wiring are exactly the
    parts most likely to be wrong, and a test app that skipped them would
    prove nothing about the one that ships.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.api.app import create_app
from app.auth.jwt import TokenVerifier
from app.config import settings as base_settings
from app.db.repositories.conversations import Conversation
from app.sql import provenance
from app.tools import charts as chart_tools

ISSUER = "https://issuer.test"
AUDIENCE = "marq-agent-test"


# ============================================================
# Token issuing
# ============================================================


class TokenIssuer:
    """
    A throwaway RSA keypair that mints tokens the API will accept.

    Generating a real key and signing a real token — rather than stubbing
    `TokenVerifier` — means these tests exercise the verification path that
    ships, including expiry, audience and algorithm pinning.
    """

    def __init__(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        self.private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        self.public_pem = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )

    def token(
        self,
        subject: str = "employee-1",
        expires_in: int = 300,
        issuer: str = ISSUER,
        audience: str = AUDIENCE,
        algorithm: str = "RS256",
        **extra: Any,
    ) -> str:
        claims = {
            "sub": subject,
            "iss": issuer,
            "aud": audience,
            "exp": int(time.time()) + expires_in,
            **extra,
        }

        return jwt.encode(claims, self.private_pem, algorithm=algorithm)

    def auth(self, subject: str = "employee-1", **kwargs: Any) -> dict[str, str]:
        """An Authorization header for this subject."""

        return {"Authorization": f"Bearer {self.token(subject, **kwargs)}"}

    def settings(self, **overrides: Any):
        return base_settings.model_copy(
            update={
                "jwt_algorithm": "RS256",
                "jwt_public_key": self.public_pem,
                "jwt_issuer": ISSUER,
                "jwt_audience": AUDIENCE,
                "auth_dev_mode": False,
                **overrides,
            }
        )

    def verifier(self, **overrides: Any) -> TokenVerifier:
        return TokenVerifier(self.settings(**overrides))


# ============================================================
# Fakes for what the lifespan would have built
# ============================================================


@dataclass
class StubGraph:
    """
    Stands in for the compiled supervisor graph.

    Records every invocation so a test can assert on what the API *passed*
    rather than only on what came back — which is the point for the identity
    tests: the question is whether `requester_id` and `workspace_id` reached
    the graph correctly, not whether the answer was good.
    """

    answer: str = "There are 315 deals in total."
    route: str = "deals"
    tool_calls: list[str] = field(default_factory=lambda: ["sql_query"])
    calls: list[dict[str, Any]] = field(default_factory=list)
    raises: Exception | None = None

    # [claude] SQL the stub pretends to have run, so the provenance path can
    # be asserted through HTTP. The real tool records the same way; see
    # app/sql/provenance.py.
    sql: str | None = "SELECT count(*) AS deals_count FROM deals"

    # A chart the stub pretends to have drawn, so the passthrough can be
    # asserted through HTTP.
    chart: dict | None = None

    # [claude] How the stubbed turn ended. Set it to `out_of_steps` to
    # replay the case the reason exists for — an agent that hit its ceiling
    # and apologised in prose, which is otherwise indistinguishable from an
    # answer. `None` replays a graph that published no reason at all.
    stop_reason: str | None = "completed"

    def _record_sql(self) -> None:
        if self.sql:
            provenance.record(
                sql=self.sql, question="stubbed", rows_available=1
            )

        if self.chart:
            chart_tools.record(self.chart)

    def _messages(self) -> list[Any]:
        return [
            HumanMessage(content="(question)"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": {}, "id": f"call-{i}"}
                    for i, name in enumerate(self.tool_calls)
                ],
            ),
            ToolMessage(content="(rows)", tool_call_id="call-0"),
            AIMessage(content=self.answer),
        ]

    async def ainvoke(self, state, config=None):
        self.calls.append({"state": state, "config": config})

        if self.raises is not None:
            raise self.raises

        self._record_sql()

        # [claude] `trace` carries the tool names, as the real graph now
        # does. The transcript no longer holds the agent's working — tool
        # payloads never enter shared state — so `tools_used` is read from
        # here rather than mined out of `messages`. See state.py.
        result = {
            "messages": self._messages(),
            "route": self.route,
            "trace": list(self.tool_calls),
        }

        if self.stop_reason is not None:
            result["stop_reason"] = self.stop_reason

        return result

    async def astream_events(self, state, config=None, version=None):
        """
        Replays the event shape a real run produces.

        [claude] Deliberately includes the two things that must be filtered
        out — a supervisor token carrying the route name, and a model token
        carrying generated SQL emitted between the tool boundaries. A stub
        that only emitted clean tokens would let the filter rot silently.
        """

        self.calls.append({"state": state, "config": config})

        if self.raises is not None:
            raise self.raises

        def chunk(text):
            return {"data": {"chunk": AIMessage(content=text)}}

        yield {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "metadata": {"langgraph_node": "supervisor"},
            **chunk(self.route),
        }
        # [claude] BOTH supervisor chain-end events, because the real graph
        # emits both and they are different shapes. The earlier stub emitted
        # only the dict, so a handler that crashed on the bare string passed
        # every test here and failed on the first real request.
        yield {
            "event": "on_chain_end",
            "name": "Unnamed",
            "metadata": {"langgraph_node": "supervisor"},
            "data": {"output": self.route},
        }
        yield {
            "event": "on_chain_end",
            "name": "supervisor",
            "metadata": {"langgraph_node": "supervisor"},
            "data": {"output": {"route": self.route}},
        }

        for name in self.tool_calls:
            self._record_sql()

            yield {
                "event": "on_tool_start",
                "name": name,
                "metadata": {"langgraph_node": "tools"},
                "data": {},
            }
            # The SQL agent, thinking inside the tool. Must not be emitted.
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "metadata": {"langgraph_node": "model"},
                **chunk("SELECT count(*) FROM deals"),
            }
            yield {
                "event": "on_tool_end",
                "name": name,
                "metadata": {"langgraph_node": "tools"},
                "data": {},
            }

        for word in self.answer.split(" "):
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "metadata": {"langgraph_node": "model"},
                **chunk(word + " "),
            }

        # [claude] The node that publishes how the turn ended, last, exactly
        # as the real graph does — `synthesise` is the supervisor graph's
        # finish point and the only node that writes the channel.
        if self.stop_reason is not None:
            yield {
                "event": "on_chain_end",
                "name": "synthesise",
                "metadata": {"langgraph_node": "synthesise"},
                "data": {"output": {"stop_reason": self.stop_reason}},
            }


class FakeConversations:
    """In-memory stand-in for ConversationRepository, same scoping rules."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Conversation] = {}
        self.keys: dict[tuple[str, str], str] = {}
        # thread_key -> [provenance per turn], mirroring conversation_turns.
        self.turns: dict[str, list] = {}

    async def record_turn(
        self, subject, thread_id, thread_key, title=None, provenance=None
    ):
        now = datetime.now(UTC)
        existing = self.rows.get((subject, thread_id))

        if existing is None:
            self.rows[(subject, thread_id)] = Conversation(
                thread_id=thread_id,
                title=title,
                created_at=now,
                updated_at=now,
                turn_count=1,
            )
        else:
            self.rows[(subject, thread_id)] = Conversation(
                thread_id=thread_id,
                # Title is written once — the conversation keeps the name of
                # the question that started it.
                title=existing.title or title,
                created_at=existing.created_at,
                updated_at=now,
                turn_count=existing.turn_count + 1,
            )

        self.keys[(subject, thread_id)] = thread_key
        self.turns.setdefault(thread_key, []).append(list(provenance or []))

        return self.rows[(subject, thread_id)].turn_count

    async def provenance_for(self, thread_key):
        return self.turns.get(thread_key, [])

    async def list_for(self, subject, limit=50, offset=0):
        owned = [
            row for (owner, _), row in self.rows.items() if owner == subject
        ]
        owned.sort(key=lambda row: row.updated_at, reverse=True)

        return owned[offset : offset + limit]

    async def get(self, subject, thread_id):
        return self.rows.get((subject, thread_id))

    async def delete(self, subject, thread_id):
        return self.rows.pop((subject, thread_id), None) is not None


@dataclass
class StubSaver:
    """A checkpointer that returns whatever messages a test planted."""

    threads: dict[str, list[Any]] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)

    async def aget_tuple(self, config):
        thread_key = config["configurable"]["thread_id"]

        if thread_key not in self.threads:
            return None

        class Snapshot:
            checkpoint = {
                "channel_values": {"messages": self.threads[thread_key]}
            }

        return Snapshot()

    async def adelete_thread(self, thread_key):
        self.deleted.append(thread_key)
        self.threads.pop(thread_key, None)


@dataclass
class StubCheckpointerHandle:
    saver: Any
    is_persistent: bool = True


# ============================================================
# The app under test
# ============================================================


def build_app(
    *,
    verifier: TokenVerifier,
    graph: Any = None,
    conversations: Any = None,
    workspace_service: Any = None,
    saver: Any = None,
):
    """
    `create_app()` with the lifespan's work injected rather than performed.

    Returns (app, parts) so a test can reach the stubs it was given.
    """

    app = create_app()

    parts = {
        "graph": graph if graph is not None else StubGraph(),
        "conversations": (
            conversations if conversations is not None else FakeConversations()
        ),
        "saver": saver if saver is not None else StubSaver(),
        "workspace_service": workspace_service,
    }

    app.state.verifier = verifier
    app.state.graph = parts["graph"]
    app.state.conversations = parts["conversations"]
    app.state.workspace_service = parts["workspace_service"]
    app.state.checkpointer = StubCheckpointerHandle(saver=parts["saver"])

    return app, parts


def client(app):
    """
    An httpx client speaking ASGI to the app, without a lifespan.

    [claude] `raise_app_exceptions=False` is what a real server does, not a
    way of hiding failures.

    Starlette's ServerErrorMiddleware handles an unexpected exception by
    building the 500 response *and* re-raising, so the process logs the
    traceback. Under uvicorn the client still receives the 500; under
    ASGITransport's default the exception is re-raised into the test instead,
    so a test asserting "the caller gets a sanitised 500" could never see the
    response it is asserting about. Turning it off makes the test observe
    what a browser observes.
    """

    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


def sse_events(text: str) -> list[tuple[str, str]]:
    """Parse an SSE body into (event, data) pairs."""

    events: list[tuple[str, str]] = []
    name: str | None = None

    for line in text.splitlines():
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            events.append((name or "message", line[len("data:") :].strip()))
            name = None

    return events


__all__ = [
    "AUDIENCE",
    "ISSUER",
    "FakeConversations",
    "StubCheckpointerHandle",
    "StubGraph",
    "StubSaver",
    "TokenIssuer",
    "build_app",
    "client",
    "sse_events",
]
