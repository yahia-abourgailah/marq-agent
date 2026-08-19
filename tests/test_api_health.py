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


# ============================================================
# What readiness says when something is actually broken
# ============================================================


@pytest.mark.asyncio
async def test_an_unreachable_database_fails_readiness(issuer, monkeypatch):
    """
    [claude] The branch that matters most.

    A readiness endpoint reporting healthy while the database is down is
    worse than no endpoint at all: the orchestrator keeps sending traffic to
    an instance that cannot answer a single question.
    """

    from app.api.routes import health as module

    class DeadPool:
        def connection(self):
            raise ConnectionError("could not connect to 10.10.67.77:5432")

    monkeypatch.setattr(module, "app_db", DeadPool())

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/health/ready")

    body = response.json()

    assert response.status_code == 503
    assert body["ready"] is False
    assert body["database"]["ok"] is False

    # Type only — a driver error's text carries DSN fragments.
    assert "10.10.67.77" not in response.text
    assert body["database"]["detail"] == "ConnectionError"


@pytest.mark.asyncio
async def test_readiness_names_which_role_the_database_uses(issuer, monkeypatch):
    """
    Reports what the connection *is*, because configuration and reality came
    apart once already: `marq_agent_ro` existed, looked configured, and had
    no grants at all.
    """

    from app.api.routes import health as module

    class Cursor:
        async def execute(self, *a):
            return None

        async def fetchone(self):
            return {"ok": 1}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Pool:
        is_read_only = True

        def connection(self):
            return Conn()

    monkeypatch.setattr(module, "app_db", Pool())

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/health/ready")

    assert response.json()["database"]["detail"] == "read-only role"


@pytest.mark.asyncio
async def test_an_unconfigured_model_fails_readiness(
    issuer, monkeypatch, stub_database
):
    """
    A missing model endpoint is a configuration error, and it should stop
    traffic rather than surface as a 500 on the first question.
    """

    from app.api.routes import health as module
    from app.config import settings as real

    monkeypatch.setattr(
        module, "settings", real.model_copy(update={"model_base_url": ""})
    )

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/health/ready")

    body = response.json()

    assert response.status_code == 503
    assert body["model"]["ok"] is False
    assert body["model"]["detail"] == "not configured"


@pytest.mark.asyncio
async def test_a_vector_index_that_raises_is_reported_not_fatal(
    issuer, stub_database
):
    """
    Qdrant being unreachable costs search only. Files still ingest, parse
    and reconcile, so it is reported and does not gate readiness.
    """

    class BrokenService:
        @property
        def index(self):
            raise RuntimeError("qdrant unreachable")

    app, _ = build_app(
        verifier=issuer.verifier(), workspace_service=BrokenService()
    )

    async with client(app) as http:
        response = await http.get("/health/ready")

    body = response.json()

    assert body["vector_index"]["ok"] is False
    assert body["vector_index"]["detail"] == "RuntimeError"
    # Reported, but readiness still turns on database + checkpointer + model.
    assert body["ready"] is True


@pytest.mark.asyncio
async def test_readiness_without_a_checkpointer_at_all(issuer, stub_database):
    """The state before the lifespan has finished, or after a failed boot."""

    app, _ = build_app(verifier=issuer.verifier())
    app.state.checkpointer = None

    async with client(app) as http:
        response = await http.get("/health/ready")

    body = response.json()

    assert response.status_code == 503
    assert body["checkpointer"]["ok"] is False
    assert body["checkpointer"]["detail"] == "not initialised"


# ============================================================
# The local UI
# ============================================================


@pytest.mark.asyncio
async def test_the_ui_is_served_at_the_root(issuer):
    """
    Served by the API itself, which is the point: same-origin, so it works
    with `CORS_ORIGINS` empty and cannot be broken by a missing entry.
    """

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "MARQ" in response.text
    # Talks to the real endpoints rather than a mock.
    assert "/v1/chat/stream" in response.text


@pytest.mark.asyncio
async def test_the_ui_needs_no_token_but_carries_no_data(issuer):
    """
    The page is static and holds no secrets — every request it makes still
    needs a verified token. Asserted so nobody later "helpfully" embeds one.
    """

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/")

    body = response.text

    assert "Bearer ey" not in body
    assert "BEGIN PRIVATE KEY" not in body
    assert "postgres" not in body.lower()


def test_the_ui_can_be_switched_off(issuer, monkeypatch):
    """
    Off in production: the front end there is the company website, and two
    UIs answering on one host is a way to confuse whoever is debugging.
    """

    from app.api import app as module
    from app.config import settings as real

    monkeypatch.setattr(
        module, "settings", real.model_copy(update={"serve_ui": False})
    )

    api = module.create_app()

    assert not any(getattr(r, "path", None) == "/" for r in api.routes)


@pytest.mark.asyncio
async def test_the_ui_never_shadows_an_api_route(issuer):
    """
    Registered last and only at `/`, so mounting it cannot capture a route
    the front end depends on.
    """

    app, _ = build_app(verifier=issuer.verifier())

    paths = set(app.openapi()["paths"])

    assert "/v1/chat" in paths
    assert "/health/ready" in paths
    assert "/" not in paths  # excluded from the schema, and not an API route
