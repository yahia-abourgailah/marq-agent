"""
[claude] Conversation history, and who can reach it.

The isolation tests here are the same shape as the workspace isolation suite:
the interesting case is not "a user sees their own conversations" but "a user
who guesses somebody else's thread id sees nothing, and cannot delete it".
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.auth.principal import Principal
from tests.api_support import (
    FakeConversations,
    StubGraph,
    StubSaver,
    TokenIssuer,
    build_app,
    client,
)


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


async def _seed(conversations, subject, thread_id, title="a question"):
    await conversations.record_turn(
        subject=subject,
        thread_id=thread_id,
        thread_key=Principal(subject).thread_key(thread_id),
        title=title,
    )


@pytest.mark.asyncio
async def test_a_caller_sees_only_their_own_conversations(issuer):
    conversations = FakeConversations()
    await _seed(conversations, "alice@example.com", "a1", "alice's question")
    await _seed(conversations, "bob@example.com", "b1", "bob's question")

    app, _ = build_app(verifier=issuer.verifier(), conversations=conversations)

    async with client(app) as http:
        response = await http.get(
            "/v1/threads", headers=issuer.auth("alice@example.com")
        )

    listed = response.json()["conversations"]

    assert [row["thread_id"] for row in listed] == ["a1"]
    assert listed[0]["title"] == "alice's question"


@pytest.mark.asyncio
async def test_another_employees_thread_reads_as_absent(issuer):
    """
    404, not 403.

    [claude] A 403 confirms the thread exists. Answering "not found" for both
    an unknown id and someone else's means a caller cannot use the status
    code to enumerate which conversations are real.
    """

    conversations = FakeConversations()
    await _seed(conversations, "alice@example.com", "a1")

    app, _ = build_app(verifier=issuer.verifier(), conversations=conversations)

    async with client(app) as http:
        theirs = await http.get(
            "/v1/threads/a1", headers=issuer.auth("bob@example.com")
        )
        nonexistent = await http.get(
            "/v1/threads/nope", headers=issuer.auth("bob@example.com")
        )

    assert theirs.status_code == 404
    assert nonexistent.status_code == 404
    assert theirs.json()["error"]["code"] == "not_found"

    # Indistinguishable apart from the request id, which is per-request by
    # design. Compared field by field rather than whole-body: an equality
    # against a dict containing request_id can only pass by accident.
    def without_request_id(response):
        error = dict(response.json()["error"])
        error.pop("request_id")
        return error

    assert without_request_id(theirs) == without_request_id(nonexistent)


@pytest.mark.asyncio
async def test_another_employees_thread_cannot_be_deleted(issuer):
    conversations = FakeConversations()
    await _seed(conversations, "alice@example.com", "a1")

    app, parts = build_app(
        verifier=issuer.verifier(), conversations=conversations
    )

    async with client(app) as http:
        response = await http.delete(
            "/v1/threads/a1", headers=issuer.auth("bob@example.com")
        )

    assert response.status_code == 404
    # Still there, and no checkpoint deletion was attempted.
    assert await conversations.get("alice@example.com", "a1") is not None
    assert parts["saver"].deleted == []


@pytest.mark.asyncio
async def test_replaying_a_conversation_returns_its_messages(issuer):
    conversations = FakeConversations()
    await _seed(conversations, "alice@example.com", "a1")

    saver = StubSaver(
        threads={
            Principal("alice@example.com").thread_key("a1"): [
                HumanMessage(content="How many deals?"),
                AIMessage(content=""),
                ToolMessage(content="[{'count': 315}]", tool_call_id="c0"),
                AIMessage(content="There are 315 deals."),
            ]
        }
    )

    app, _ = build_app(
        verifier=issuer.verifier(),
        conversations=conversations,
        saver=saver,
    )

    async with client(app) as http:
        response = await http.get(
            "/v1/threads/a1", headers=issuer.auth("alice@example.com")
        )

    messages = response.json()["messages"]

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "How many deals?"
    assert messages[1]["content"] == "There are 315 deals."


@pytest.mark.asyncio
async def test_raw_tool_results_are_not_replayed(issuer):
    """
    [claude] Tool messages are raw CRM rows. The answer that quoted them is
    already in the transcript, so re-serving the rows themselves would put
    more data in the history view than the conversation ever showed.
    """

    conversations = FakeConversations()
    await _seed(conversations, "alice@example.com", "a1")

    saver = StubSaver(
        threads={
            Principal("alice@example.com").thread_key("a1"): [
                HumanMessage(content="who owns the most deals"),
                ToolMessage(
                    content="[{'name': 'Sara', 'phone': '+20100000000'}]",
                    tool_call_id="c0",
                ),
                AIMessage(content="Sara owns the most."),
            ]
        }
    )

    app, _ = build_app(
        verifier=issuer.verifier(), conversations=conversations, saver=saver
    )

    async with client(app) as http:
        response = await http.get(
            "/v1/threads/a1", headers=issuer.auth("alice@example.com")
        )

    assert "+20100000000" not in response.text
    assert [m["role"] for m in response.json()["messages"]] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_deleting_removes_the_checkpoints_before_the_index_row(issuer):
    """
    Order matters: index-row-first would orphan the messages.

    They would be unlistable, undeletable through this API, and still
    holding CRM answers. This way round a half-failure leaves a listed
    conversation that can be deleted again.
    """

    conversations = FakeConversations()
    await _seed(conversations, "alice@example.com", "a1")

    app, parts = build_app(
        verifier=issuer.verifier(), conversations=conversations
    )

    async with client(app) as http:
        response = await http.delete(
            "/v1/threads/a1", headers=issuer.auth("alice@example.com")
        )

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert parts["saver"].deleted == [
        Principal("alice@example.com").thread_key("a1")
    ]
    assert await conversations.get("alice@example.com", "a1") is None


@pytest.mark.asyncio
async def test_a_conversation_appears_after_a_turn(issuer):
    """The list endpoint and the chat endpoint agree — end to end."""

    conversations = FakeConversations()
    app, _ = build_app(
        verifier=issuer.verifier(),
        graph=StubGraph(),
        conversations=conversations,
    )

    async with client(app) as http:
        turn = await http.post(
            "/v1/chat",
            json={"message": "How many deals closed in July?"},
            headers=issuer.auth("employee-9"),
        )
        listed = await http.get(
            "/v1/threads", headers=issuer.auth("employee-9")
        )

    thread_id = turn.json()["thread_id"]
    rows = listed.json()["conversations"]

    assert [row["thread_id"] for row in rows] == [thread_id]
    assert rows[0]["title"] == "How many deals closed in July?"
    assert rows[0]["turn_count"] == 1


@pytest.mark.asyncio
async def test_listing_is_paginated(issuer):
    conversations = FakeConversations()

    for index in range(5):
        await _seed(conversations, "employee-1", f"t{index}")

    app, _ = build_app(verifier=issuer.verifier(), conversations=conversations)

    async with client(app) as http:
        page = await http.get(
            "/v1/threads?limit=2", headers=issuer.auth("employee-1")
        )
        bad = await http.get(
            "/v1/threads?limit=0", headers=issuer.auth("employee-1")
        )

    assert len(page.json()["conversations"]) == 2
    assert bad.status_code == 422


# ============================================================
# Provenance survives a replay
# ============================================================


@pytest.mark.asyncio
async def test_a_replayed_answer_still_carries_its_sql(issuer):
    """
    [claude] The gap this closes.

    Provenance was captured per request and returned in the chat response,
    then lost the moment the conversation was reopened — so a replayed
    answer was back to being a number the reader had to trust, which is the
    exact thing the feature exists to prevent.
    """

    conversations = FakeConversations()
    graph = StubGraph(sql="SELECT count(*) AS deals_count FROM deals")

    app, parts = build_app(
        verifier=issuer.verifier(), graph=graph, conversations=conversations
    )

    async with client(app) as http:
        turn = await http.post(
            "/v1/chat",
            json={"message": "How many deals?", "thread_id": "t1"},
            headers=issuer.auth("alice@example.com"),
        )

        # Plant the transcript the checkpointer would hold.
        parts["saver"].threads[Principal("alice@example.com").thread_key("t1")] = [
            HumanMessage(content="How many deals?"),
            AIMessage(content="There are 315 deals in total."),
        ]

        replay = await http.get(
            "/v1/threads/t1", headers=issuer.auth("alice@example.com")
        )

    live = turn.json()["provenance"]
    messages = replay.json()["messages"]

    assert live[0]["sql"] == "SELECT count(*) AS deals_count FROM deals"

    assistant = [m for m in messages if m["role"] == "assistant"]

    assert assistant[0]["provenance"] == live
    # The question carries none — only answers have queries behind them.
    assert [m for m in messages if m["role"] == "user"][0]["provenance"] == []


@pytest.mark.asyncio
async def test_each_turn_keeps_its_own_queries(issuer):
    """
    Turn N's provenance must attach to answer N. Off-by-one here pairs an
    answer with the query from a different question — which looks entirely
    plausible and is worse than showing nothing.
    """

    conversations = FakeConversations()
    graph = StubGraph()
    app, parts = build_app(
        verifier=issuer.verifier(), graph=graph, conversations=conversations
    )
    subject = "alice@example.com"

    async with client(app) as http:
        for n in (1, 2, 3):
            graph.sql = f"SELECT {n}"
            await http.post(
                "/v1/chat",
                json={"message": f"question {n}", "thread_id": "t1"},
                headers=issuer.auth(subject),
            )

        parts["saver"].threads[Principal(subject).thread_key("t1")] = [
            HumanMessage(content="question 1"),
            AIMessage(content="answer 1"),
            HumanMessage(content="question 2"),
            AIMessage(content="answer 2"),
            HumanMessage(content="question 3"),
            AIMessage(content="answer 3"),
        ]

        replay = await http.get("/v1/threads/t1", headers=issuer.auth(subject))

    assistant = [m for m in replay.json()["messages"] if m["role"] == "assistant"]

    assert [m["provenance"][0]["sql"] for m in assistant] == [
        "SELECT 1",
        "SELECT 2",
        "SELECT 3",
    ]


@pytest.mark.asyncio
async def test_a_turn_that_ran_no_query_replays_with_none(issuer):
    conversations = FakeConversations()
    app, parts = build_app(
        verifier=issuer.verifier(),
        graph=StubGraph(sql=None, tool_calls=[], route="out_of_scope"),
        conversations=conversations,
    )
    subject = "alice@example.com"

    async with client(app) as http:
        await http.post(
            "/v1/chat",
            json={"message": "what is the weather", "thread_id": "t1"},
            headers=issuer.auth(subject),
        )
        parts["saver"].threads[Principal(subject).thread_key("t1")] = [
            HumanMessage(content="what is the weather"),
            AIMessage(content="I can only help with CRM data."),
        ]
        replay = await http.get("/v1/threads/t1", headers=issuer.auth(subject))

    assistant = [m for m in replay.json()["messages"] if m["role"] == "assistant"]

    assert assistant[0]["provenance"] == []
