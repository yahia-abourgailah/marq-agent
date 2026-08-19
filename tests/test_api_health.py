"""
[claude] Liveness and readiness.

The readiness assertions are about a specific past failure. docs/HANDOFF.md
records a read-only role that existed, looked correctly configured, and had
no grants at all — it could read nothing. A check that read configuration
would have called that healthy, so these tests pin the behaviour that
readiness reports what a dependency *can do*.
"""

from __future__ import annotations

import pytest

from app.api.schemas import ComponentHealth
from tests.api_support import (
    StubCheckpointerHandle,
    StubSaver,
    TokenIssuer,
    build_app,
    client,
)


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


@pytest.fixture
def stub_database(monkeypatch):
    """
    [claude] Keeps the readiness tests hermetic.

    `_database()` opens a real connection through `app_db`, which is correct
    for the endpoint and wrong for this suite — `pytest` is the hermetic run
    and only `pytest -m integration` may need PostgreSQL. Left unpatched
    these passed here purely because a development database happened to be
    running, and would have behaved differently in CI.

    The live path is still exercised, by test_infrastructure's integration
    tests and by the endpoint itself.
    """

    async def fake():
        return ComponentHealth(ok=True, detail="stubbed")

    monkeypatch.setattr("app.api.routes.health._database", fake)


@pytest.mark.asyncio
async def test_liveness_needs_no_token(issuer):
    """
    A probe should not need credentials, and should not do I/O.

    A liveness check that touches the database restarts a healthy process
    when a dependency blips, turning one outage into two.
    """

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/health")

    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["version"]
    assert body["environment"]


@pytest.mark.asyncio
async def test_an_in_memory_checkpointer_is_reported_as_not_ready(
    issuer, stub_database
):
    """
    [claude] Not-ready rather than fine, deliberately.

    In-memory checkpointing loses every conversation on restart and shares
    nothing between workers, so a follow-up question routed to a second
    worker finds a thread that never existed there. That surfaces as the
    agent forgetting mid-conversation, intermittently — a model-looking bug
    with an infrastructure cause. Better it fails a probe.
    """

    app, _ = build_app(verifier=issuer.verifier())
    app.state.checkpointer = StubCheckpointerHandle(
        saver=StubSaver(), is_persistent=False
    )

    async with client(app) as http:
        response = await http.get("/health/ready")

    body = response.json()

    assert response.status_code == 503
    assert body["ready"] is False
    assert body["checkpointer"]["ok"] is False
    assert "restart" in body["checkpointer"]["detail"]


@pytest.mark.asyncio
async def test_a_missing_vector_index_does_not_block_traffic(
    issuer, stub_database
):
    """
    Search degrades; reconciliation and the CRM agent keep working.

    Refusing traffic for it would take the whole agent down because a
    file-search feature is unavailable — which is a much bigger outage than
    the one being reported.
    """

    app, _ = build_app(verifier=issuer.verifier(), workspace_service=None)

    async with client(app) as http:
        response = await http.get("/health/ready")

    body = response.json()

    assert body["vector_index"]["ok"] is False
    # The index is reported but is not part of the readiness decision.
    assert "vector_index" not in str(body["ready"])


@pytest.mark.asyncio
async def test_readiness_reports_every_component(issuer, stub_database):
    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/health/ready")

    body = response.json()

    for component in ("database", "checkpointer", "model", "vector_index"):
        assert component in body
        assert "ok" in body[component]
