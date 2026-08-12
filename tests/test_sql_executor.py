from unittest.mock import AsyncMock

import pytest

from app.sql.executor import SQLExecutor


class FakeCursor:
    def __init__(self):
        self.execute = AsyncMock()
        self.fetchall = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "name": "Ali",
                }
            ]
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor

    def cursor(self):
        return self.cursor_instance


class FakeConnectionContext:
    def __init__(self, connection):
        self.connection_instance = connection

    async def __aenter__(self):
        return self.connection_instance

    async def __aexit__(self, exc_type, exc, tb):
        pass


class FakeDatabase:
    def __init__(self, connection):
        self.connection_instance = connection

    def connection(self):
        return FakeConnectionContext(
            self.connection_instance
        )


@pytest.mark.asyncio
async def test_execute_like_query_with_empty_params():
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    database = FakeDatabase(connection)

    executor = SQLExecutor(database)

    query = """
        SELECT id, name
        FROM deals
        WHERE name LIKE '%Ali%';
    """

    result = await executor.execute(query)

    assert result == [
        {
            "id": 1,
            "name": "Ali",
        }
    ]

    cursor.execute.assert_awaited_once_with(
        query,
        None,
    )
