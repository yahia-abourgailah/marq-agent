from __future__ import annotations

import pytest

from app.db.repositories.sql import SQLRepository


class FakeExecutor:
    def __init__(self):
        self.query = None
        self.params = None
        self.requester_id = None

    async def execute(self, query, params=(), requester_id=None):
        self.query = query
        self.params = params
        self.requester_id = requester_id

        return [
            {
                "id": 1,
                "name": "Test Deal",
            }
        ]


class FakeGuard:
    def __init__(self):
        self.query = None

    def validate(self, query):
        self.query = query
        return query


@pytest.mark.asyncio
async def test_execute_read_validates_and_executes_query():
    executor = FakeExecutor()
    guard = FakeGuard()

    repository = SQLRepository(
        executor=executor,
        guard=guard,
    )

    query = """
        SELECT id, name
        FROM deals
    """

    result = await repository.execute_read(query)

    assert result == [
        {
            "id": 1,
            "name": "Test Deal",
        }
    ]

    assert guard.query == query
    assert executor.query == query
    assert executor.params == ()


@pytest.mark.asyncio
async def test_execute_read_passes_parameters():
    executor = FakeExecutor()
    guard = FakeGuard()

    repository = SQLRepository(
        executor=executor,
        guard=guard,
    )

    query = """
        SELECT id, name
        FROM deals
        WHERE id = %s
    """

    params = (1042,)

    result = await repository.execute_read(
        query,
        params=params,
    )

    assert result == [
        {
            "id": 1,
            "name": "Test Deal",
        }
    ]

    assert guard.query == query
    assert executor.query == query
    assert executor.params == params
