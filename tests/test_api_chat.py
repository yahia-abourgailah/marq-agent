"""
[claude] The chat endpoints, JSON and SSE.

The streaming tests are the substantial half. Filtering is not cosmetic
there: the raw event feed of one real question carries the supervisor's route
decision and the SQL Agent's generated SELECT alongside the answer, and both
share a node name with tokens the user *should* see. `StubGraph.astream_events`
replays that exact shape, including the parts that must be dropped, so the
filter cannot rot silently into passing.
"""

from __future__ import annotations

import json

import pytest

from tests.api_support import (
    FakeConversations,
    StubGraph,
    TokenIssuer,
    build_app,
    client,
    sse_events,
)


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


# ============================================================
# The plain JSON turn
# ============================================================


@pytest.mark.asyncio
async def test_a_question_returns_an_answer_with_its_route(issuer):
    graph = StubGraph(answer="There are 315 deals in total.", route="deals")
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "How many deals are there?"},
            headers=issuer.auth(),
        )

    body = response.json()

    assert response.status_code == 200
    assert body["answer"] == "There are 315 deals in total."
    assert body["route"] == "deals"
    assert body["tools_used"] == ["sql_query"]
    assert body["thread_id"]


@pytest.mark.asyncio
async def test_omitting_a_thread_id_starts_a_new_conversation(issuer):
    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        first = await http.post(
            "/v1/chat", json={"message": "hi"}, headers=issuer.auth()
        )
        second = await http.post(
            "/v1/chat", json={"message": "hi"}, headers=issuer.auth()
        )

    assert first.json()["thread_id"] != second.json()["thread_id"]


@pytest.mark.asyncio
async def test_a_thread_id_is_namespaced_before_it_reaches_the_graph(issuer):
    """
    The id the client chose is never the checkpointer's key.

    [claude] Asserted on the config the graph received. A client-chosen
    thread id used directly would let anyone read anyone's conversation by
    guessing an id, so this is the load-bearing line rather than a detail.
    """

    graph = StubGraph()
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        await http.post(
            "/v1/chat",
            json={"message": "hi", "thread_id": "today"},
            headers=issuer.auth("alice@example.com"),
        )

    thread_key = graph.calls[-1]["config"]["configurable"]["thread_id"]

    assert thread_key != "today"
    assert thread_key.endswith(":today")
    assert "alice@example.com" not in thread_key


@pytest.mark.asyncio
async def test_the_same_thread_id_from_two_employees_does_not_collide(issuer):
    graph = StubGraph()
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        for subject in ("alice@example.com", "bob@example.com"):
            await http.post(
                "/v1/chat",
                json={"message": "hi", "thread_id": "today"},
                headers=issuer.auth(subject),
            )

    keys = [
        call["config"]["configurable"]["thread_id"] for call in graph.calls
    ]

    assert keys[0] != keys[1]


@pytest.mark.asyncio
async def test_a_completed_turn_is_recorded_once(issuer):
    conversations = FakeConversations()
    app, _ = build_app(
        verifier=issuer.verifier(), conversations=conversations
    )

    async with client(app) as http:
        await http.post(
            "/v1/chat",
            json={"message": "How many deals did we close?", "thread_id": "t1"},
            headers=issuer.auth("employee-1"),
        )
        await http.post(
            "/v1/chat",
            json={"message": "and last month?", "thread_id": "t1"},
            headers=issuer.auth("employee-1"),
        )

    row = await conversations.get("employee-1", "t1")

    assert row.turn_count == 2
    # Named after the question that started it, not the most recent one.
    assert row.title == "How many deals did we close?"


@pytest.mark.asyncio
async def test_a_failed_turn_leaves_no_conversation_behind(issuer):
    """
    A question that raised should not appear in the user's history.

    Recording before the answer would leave an empty conversation in the
    list every time anything went wrong.
    """

    conversations = FakeConversations()
    app, _ = build_app(
        verifier=issuer.verifier(),
        graph=StubGraph(raises=RuntimeError("model exploded")),
        conversations=conversations,
    )

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi", "thread_id": "t1"},
            headers=issuer.auth("employee-1"),
        )

    assert response.status_code == 500
    assert await conversations.get("employee-1", "t1") is None


@pytest.mark.asyncio
async def test_an_internal_failure_leaks_nothing(issuer):
    """
    The rule from design decision 6, at the HTTP edge.

    An unhandled driver error's text carries DSN fragments; a parser error
    carries file paths. The caller gets a fixed sentence and a request id.
    """

    secret = "postgresql://marq:hunter2@10.10.67.77:5432/mytai"

    app, _ = build_app(
        verifier=issuer.verifier(),
        graph=StubGraph(raises=RuntimeError(f"connection failed: {secret}")),
    )

    async with client(app) as http:
        response = await http.post(
            "/v1/chat", json={"message": "hi"}, headers=issuer.auth()
        )

    body = response.text

    assert response.status_code == 500
    assert "hunter2" not in body
    assert "10.10.67.77" not in body
    assert "RuntimeError" not in body
    assert response.json()["error"]["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"message": ""},
        {"message": "   " * 0},
        {"message": "x" * 4001},
        {"message": "hi", "thread_id": "../../etc/passwd"},
        {"message": "hi", "thread_id": ""},
    ],
)
async def test_invalid_bodies_are_refused(issuer, body):
    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.post(
            "/v1/chat", json=body, headers=issuer.auth()
        )

    assert response.status_code == 422


# ============================================================
# Streaming
# ============================================================


@pytest.mark.asyncio
async def test_the_stream_never_emits_the_route_or_the_generated_sql(issuer):
    """
    The reason the filter exists, asserted directly.

    Measured against the live graph before it was written: one question
    emitted `'deals'` from the supervisor and a full `SELECT ...` from the
    SQL Agent, both from nodes that also produce answer tokens. Showing
    either to a user would leak how the system works and, in the SQL case,
    column names from a schema they never see.
    """

    graph = StubGraph(answer="There are 315 deals in total.", route="deals")
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        response = await http.post(
            "/v1/chat/stream",
            json={"message": "How many deals?"},
            headers=issuer.auth(),
        )

    events = sse_events(response.text)
    streamed = "".join(
        json.loads(data)["text"] for name, data in events if name == "token"
    )

    assert streamed.strip() == "There are 315 deals in total."
    assert "SELECT" not in streamed
    assert "deals_count" not in streamed


@pytest.mark.asyncio
async def test_the_stream_ends_with_the_whole_answer(issuer):
    """
    A client can ignore `token` entirely and still be correct — which is what
    makes the stream usable from a script and survivable across a reconnect.
    """

    graph = StubGraph(answer="Franchise 4 cancelled 10 of 33.", route="deals")
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        response = await http.post(
            "/v1/chat/stream",
            json={"message": "which franchises worry you"},
            headers=issuer.auth(),
        )

    events = sse_events(response.text)
    final = json.loads(
        next(data for name, data in events if name == "final")
    )

    assert final["answer"] == "Franchise 4 cancelled 10 of 33."
    assert final["route"] == "deals"
    assert final["tools_used"] == ["sql_query"]


@pytest.mark.asyncio
async def test_the_stream_announces_the_route_and_each_tool(issuer):
    """Progress the UI can show while a slow turn is still running."""

    graph = StubGraph(tool_calls=["workspace_files", "compare_with_crm"])
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        response = await http.post(
            "/v1/chat/stream",
            json={"message": "does my sheet match"},
            headers=issuer.auth(),
        )

    events = sse_events(response.text)
    names = [name for name, _ in events]
    tools = [
        json.loads(data)["name"] for name, data in events if name == "tool"
    ]

    assert names[0] == "start"
    assert "route" in names
    assert tools == ["workspace_files", "compare_with_crm"]


@pytest.mark.asyncio
async def test_a_stream_that_fails_reports_an_error_event(issuer):
    """
    A stream cannot change its status code once it has started, so the
    failure has to arrive as an event — and still say nothing internal.
    """

    app, _ = build_app(
        verifier=issuer.verifier(),
        graph=StubGraph(raises=RuntimeError("password=hunter2")),
    )

    async with client(app) as http:
        response = await http.post(
            "/v1/chat/stream",
            json={"message": "hi"},
            headers=issuer.auth(),
        )

    events = sse_events(response.text)
    errors = [json.loads(data) for name, data in events if name == "error"]

    assert response.status_code == 200
    assert len(errors) == 1
    assert errors[0]["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert "final" not in [name for name, _ in events]


@pytest.mark.asyncio
async def test_a_failed_stream_records_no_conversation(issuer):
    conversations = FakeConversations()
    app, _ = build_app(
        verifier=issuer.verifier(),
        graph=StubGraph(raises=RuntimeError("boom")),
        conversations=conversations,
    )

    async with client(app) as http:
        await http.post(
            "/v1/chat/stream",
            json={"message": "hi", "thread_id": "t1"},
            headers=issuer.auth("employee-1"),
        )

    assert await conversations.get("employee-1", "t1") is None


@pytest.mark.asyncio
async def test_streaming_and_json_send_the_graph_the_same_identity(issuer):
    """
    The two endpoints share `graph_input`, so identity cannot drift between
    them. Asserted rather than assumed, because a second code path is
    exactly where a security property gets dropped.
    """

    graph = StubGraph()
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        await http.post(
            "/v1/chat",
            json={"message": "hi", "thread_id": "t1"},
            headers=issuer.auth("employee-7"),
        )
        await http.post(
            "/v1/chat/stream",
            json={"message": "hi", "thread_id": "t1"},
            headers=issuer.auth("employee-7"),
        )

    json_call, stream_call = graph.calls

    assert json_call["state"]["requester_id"] == stream_call["state"]["requester_id"]
    assert json_call["state"]["workspace_id"] == stream_call["state"]["workspace_id"]
    assert (
        json_call["config"]["configurable"]["thread_id"]
        == stream_call["config"]["configurable"]["thread_id"]
    )
