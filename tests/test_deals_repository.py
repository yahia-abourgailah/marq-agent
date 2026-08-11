import pytest

from app.db.repositories.deals import DealsRepository


class FakeGuard:
    def validate(self, query: str) -> str:
        return query


class FakeExecutor:
    def __init__(self):
        self.queries = []

    async def execute(self, query, params=()):
        self.queries.append((query, params))
        return [{"test": True}]


@pytest.mark.asyncio
async def test_pipeline_summary():
    executor = FakeExecutor()

    repository = DealsRepository(
        executor=executor,
        guard=FakeGuard(),
    )

    result = await repository.pipeline_summary()

    assert result == [{"test": True}]

    query, params = executor.queries[0]

    assert "GROUP BY status" in query
    assert "deleted_at IS NULL" in query
    assert params == ()


@pytest.mark.asyncio
async def test_get_deals():
    executor = FakeExecutor()

    repository = DealsRepository(
        executor=executor,
        guard=FakeGuard(),
    )

    result = await repository.get_deals(
        status="reservation",
        project_id=5,
        limit=10,
    )

    assert result == [{"test": True}]

    query, params = executor.queries[0]

    assert "status = %s" in query
    assert "project_id = %s" in query
    assert "deleted_at IS NULL" in query

    assert params == ("reservation", 5, 10)


@pytest.mark.asyncio
async def test_get_deal_detail():
    executor = FakeExecutor()

    repository = DealsRepository(
        executor=executor,
        guard=FakeGuard(),
    )

    result = await repository.get_deal_detail(1042)

    assert result == [{"test": True}]

    query, params = executor.queries[0]

    assert "id = %s" in query
    assert "deleted_at IS NULL" in query

    assert params == (1042,)


@pytest.mark.asyncio
async def test_closing_soon():
    executor = FakeExecutor()

    repository = DealsRepository(
        executor=executor,
        guard=FakeGuard(),
    )

    result = await repository.closing_soon(
        closing_before="2026-09-01",
        limit=20,
    )

    assert result == [{"test": True}]

    query, params = executor.queries[0]

    assert "expected_closing_date" in query
    assert "deleted_at IS NULL" in query
    assert "status NOT IN" in query

    assert params == ("2026-09-01", 20)