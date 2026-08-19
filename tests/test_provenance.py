"""
[claude] The queries behind an answer.

This feature exists because the agent's numbers were unfalsifiable. A user
reading "there are 315 deals" could not tell a correct answer from a
plausible one, and every defect this project has found was exactly that — a
confident wrong number rather than a crash. Returning the SQL makes the claim
checkable by the person reading it, not just by a developer with psql.

Two properties matter and are tested here:

*   the query is captured, and
*   it never enters the model's context.

The second is not decoration. `app/tools/sql.py` caps its own payload because
result width is what blows the context window, and putting a query string
into every tool result would spend tokens on something the agent wrote and
will never read back.
"""

from __future__ import annotations

import asyncio

import pytest

from app.sql import provenance


def test_nothing_is_recorded_when_nobody_is_collecting():
    """
    The default everywhere except an HTTP request.

    The CLI, the eval suites and the hermetic tests all run the same tool
    code; a collector that was always on would accumulate records nobody
    reads, in a module-level list that never empties.
    """

    provenance.record(sql="SELECT 1")  # must not raise, must not store

    with provenance.collect() as collector:
        pass

    assert collector.records == []


def test_a_query_is_recorded_inside_a_collector():
    with provenance.collect() as collector:
        provenance.record(
            sql="SELECT count(*) FROM deals",
            question="how many deals",
            rows_available=1,
        )

    assert len(collector.records) == 1
    assert collector.records[0].sql == "SELECT count(*) FROM deals"
    assert collector.records[0].rows_available == 1
    assert collector.records[0].refused is None


def test_a_refusal_is_recorded_without_sql():
    """
    "No query ran, and here is why" is more useful than silence — and it is
    the honest record of a masked column or an out-of-domain table.
    """

    with provenance.collect() as collector:
        provenance.record(question="total contract price", refused="not available")

    record = collector.records[0]

    assert record.sql is None
    assert record.refused == "not available"


def test_records_survive_nested_tasks():
    """
    [claude] The mechanism, asserted directly.

    LangGraph runs nodes and tools in its own tasks. `asyncio.Task` copies
    the context at creation, and the collector is *mutated* rather than
    rebound, so appends made several tasks deep reach the request that
    started them. If this ever stopped being true, provenance would come
    back silently empty — a feature that looks present and reports nothing.
    """

    async def deep():
        provenance.record(sql="SELECT 1", question="deep")

    async def middle():
        await asyncio.create_task(deep())

    async def main():
        with provenance.collect() as collector:
            await asyncio.create_task(middle())

        return collector

    collector = asyncio.run(main())

    assert [r.question for r in collector.records] == ["deep"]


def test_collectors_do_not_leak_into_one_another():
    """
    One request's queries must never appear in another's. The context is
    restored on the way out, so a nested collector cannot strand itself.
    """

    with provenance.collect() as outer:
        provenance.record(sql="OUTER")

        with provenance.collect() as inner:
            provenance.record(sql="INNER")

        provenance.record(sql="OUTER AGAIN")

    assert [r.sql for r in inner.records] == ["INNER"]
    assert [r.sql for r in outer.records] == ["OUTER", "OUTER AGAIN"]


@pytest.mark.asyncio
async def test_the_sql_tool_records_what_it_ran():
    """
    Through the real tool, not a stand-in for it.

    Asserts both halves at once: the query reaches the collector, and the
    tool's own payload — which is what the model sees — still does not
    contain it.
    """

    from app.tools.sql import SQLTool

    class FakeAgent:
        pass

    class FakeRepository:
        async def execute_read(self, query, params=(), requester_id=None):
            return [{"deals_count": 315}]

    tool = SQLTool(sql_agent=FakeAgent(), repository=FakeRepository())

    import app.tools.sql as module

    class Generated:
        query = "SELECT count(*) AS deals_count FROM deals"

    async def fake_generate(agent, question):
        return Generated()

    original = module.generate_sql
    module.generate_sql = fake_generate

    try:
        with provenance.collect() as collector:
            payload = await tool.query("how many deals")
    finally:
        module.generate_sql = original

    assert payload["success"] is True
    assert collector.records[0].sql == "SELECT count(*) AS deals_count FROM deals"
    assert collector.records[0].rows_available == 1

    # The model's view of the tool result carries no SQL.
    assert "SELECT" not in str(payload)
    assert "sql" not in payload


def test_provenance_can_be_switched_off(monkeypatch):
    """`EXPOSE_PROVENANCE=false` returns nothing, whatever was collected."""

    from app.api import streaming
    from app.config import settings as real

    with provenance.collect() as collector:
        provenance.record(sql="SELECT 1")

    monkeypatch.setattr(
        streaming, "settings", real.model_copy(update={"expose_provenance": False})
    )

    assert streaming.provenance_of(collector) == []

    monkeypatch.setattr(
        streaming, "settings", real.model_copy(update={"expose_provenance": True})
    )

    assert streaming.provenance_of(collector)[0]["sql"] == "SELECT 1"


def test_a_turn_with_no_collector_reports_an_empty_list():
    from app.api import streaming

    assert streaming.provenance_of(None) == []
