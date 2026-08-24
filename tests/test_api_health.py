"""
[claude] Liveness and readiness.

The readiness assertions are about a specific past failure. docs/HANDOFF.md
records a read-only role that existed, looked correctly configured, and had
no grants at all — it could read nothing. A check that read configuration
would have called that healthy, so these tests pin the behaviour that
readiness reports what a dependency *can do*.
"""

from __future__ import annotations

import re
from pathlib import Path

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
    Reports what the connection *can do*, because configuration and reality
    came apart once already: `marq_agent_ro` existed, looked configured, and
    had no grants at all.

    [claude] The probe used to read `app_db.is_read_only` — the configured
    flag — while `verify_read_only()`, documented in three places as the
    check, was called by nothing. The fake below now has to implement the
    capability methods, which is the point: a stub that cannot answer "what
    can this connection actually do" is a stub of the wrong thing.
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

        async def verify_read_only(self):
            return True

        async def rls_posture(self, tables=("deals", "leads")):
            return [
                {
                    "table": t,
                    "enabled": False,
                    "forced": False,
                    "owned_by_this_role": False,
                    "policies_for_this_role": 0,
                    "verdict": "off",
                }
                for t in tables
            ]

    monkeypatch.setattr(module, "app_db", Pool())

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/health/ready")

    detail = response.json()["database"]["detail"]

    assert detail.startswith("read-only role")
    # The standing exposure is named rather than left to be inferred from
    # its absence.
    assert "RLS not applied" in detail


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["inert", "blind"])
async def test_an_unsafe_rls_posture_fails_readiness(
    issuer, monkeypatch, verdict
):
    """
    [claude] The two states that do not announce themselves.

    `inert` — RLS enabled, this connection owns the table, FORCE not set.
    The owner bypasses RLS, so the migration ran, the policies exist and
    every row is visible to everyone. No error, no log line, and a
    deployment that looks correct.

    `blind` — RLS forced and no policy covers this role. Default-deny means
    zero rows rather than an error, so the agent reports "there are no
    deals" with total confidence.

    One is a silent exposure and the other a silent outage. Both are
    reachable by getting one environment variable wrong, which is why this
    is a readiness failure rather than a log line.
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
        is_read_only = False

        def connection(self):
            return Conn()

        async def verify_read_only(self):
            return False

        async def rls_posture(self, tables=("deals", "leads")):
            return [
                {
                    "table": "deals",
                    "enabled": True,
                    "forced": verdict == "blind",
                    "owned_by_this_role": verdict == "inert",
                    "policies_for_this_role": 0,
                    "verdict": verdict,
                }
            ]

    monkeypatch.setattr(module, "app_db", Pool())

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/health/ready")

    body = response.json()

    assert body["ready"] is False
    assert body["database"]["ok"] is False
    assert verdict in body["database"]["detail"]


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

    # [claude] The endpoint moved into app.js when the page was split, so
    # this asserts it where it now lives rather than being deleted. It is
    # the check that the UI talks to the real API rather than to a mock,
    # which is worth keeping wherever the string ends up.
    async with client(app) as http:
        script = await http.get("/app.js")

    assert script.status_code == 200
    assert "/v1/chat/stream" in script.text


@pytest.mark.asyncio
async def test_the_ui_needs_no_token_but_carries_no_data(issuer):
    """
    The page is static and holds no secrets — every request it makes still
    needs a verified token. Asserted so nobody later "helpfully" embeds one.

    [claude] Scans all three files, not just the page.

    Splitting the stylesheet and script out did not fail this test — it
    quietly reduced it to scanning 162 lines of markup, while the ~1,900
    lines where a token would actually be pasted stopped being checked at
    all. A test that keeps passing over less and less is worse than one
    that breaks, because nothing tells you it stopped working.
    """

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        served = {
            path: (await http.get(path)).text
            for path in ("/", "/app.css", "/app.js")
        }

    for path, body in served.items():
        assert "Bearer ey" not in body, f"a token is embedded in {path}"
        assert "BEGIN PRIVATE KEY" not in body, f"key material in {path}"
        assert "postgres" not in body.lower(), f"a DSN is in {path}"


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


# ============================================================
# Vendored front-end code
# ============================================================


@pytest.mark.asyncio
async def test_the_vendored_library_is_served_from_this_host(issuer):
    """
    [claude] The UI animates with Motion, and Motion is served from here
    rather than from a CDN.

    That is a privacy decision before it is a performance one. This console
    is a window onto a private CRM, and a `<script src="https://cdn…">`
    would announce to a third party, on every page load, that an internal
    tool is being used — as well as putting a public network dependency
    between an internal user and an internal service.
    """

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/vendor/motion.min.js")

    assert response.status_code == 200
    assert len(response.content) > 50_000
    # The UMD build attaches to window.Motion; a wrong file would not.
    assert b"Motion" in response.content[:400]


@pytest.mark.asyncio
async def test_the_ui_asks_for_nothing_off_this_host(issuer):
    """
    [claude] The one deliberate exception is Google Fonts, which carries
    the brand's typefaces and predates this. Everything else — scripts
    especially — must resolve to this origin, or the privacy argument above
    is decoration.

    Written as a rule rather than a habit because the tempting fix for any
    future library is a CDN tag, and it would pass every other test here.
    """

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        body = (await http.get("/")).text

    scripts = re.findall(r'<script[^>]*\ssrc=["\']([^"\']+)["\']', body)

    assert scripts, "the UI should be loading its vendored library"
    for src in scripts:
        assert not src.startswith(("http://", "https://", "//")), (
            f"{src} is fetched from another host — vendor it into "
            "static/vendor instead"
        )

    remote = re.findall(r'(?:href|src)=["\'](https?://[^"\']+)["\']', body)
    for url in remote:
        assert "fonts.googleapis.com" in url or "fonts.gstatic.com" in url, (
            f"unexpected third-party request to {url}"
        )


# ============================================================
# The page and its assets are one set
# ============================================================


@pytest.mark.asyncio
async def test_the_page_references_assets_that_actually_resolve(issuer):
    """
    [claude] The failure this catches is a rename.

    Splitting the page into three files means a path can now be wrong. A
    mistyped `href` does not raise anything: the API returns 200 for the
    page, the browser 404s the stylesheet, and the console renders as
    unstyled HTML — which reads as a CSS bug rather than a missing file.
    """

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        page = (await http.get("/")).text

        referenced = re.findall(
            r'(?:href|src)=["\'](/[^"\']+)["\']', page
        )

        assert referenced, "the page should be pulling in its own assets"
        assert "/app.css" in referenced
        assert "/app.js" in referenced

        for path in referenced:
            assert (await http.get(path)).status_code == 200, (
                f"the page references {path}, which does not resolve"
            )


@pytest.mark.asyncio
async def test_the_page_and_its_assets_cache_together(issuer):
    """
    [claude] All three carry `no-store`, and this is the rule that keeps
    the split honest.

    They only make sense as a set: the page names the classes, the
    stylesheet styles them, the script queries them by id. Cache one and
    not the others and a browser can hold a *mismatched* set — which does
    not present as a caching problem. It presents as a layout regression,
    or as controls that silently do nothing because the script is
    addressing markup that is no longer there.

    The vendored library is deliberately not included: it is pinned, it
    changes only when somebody re-vendors it, and it is the only large file
    here.
    """

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        for path in ("/", "/app.css", "/app.js"):
            response = await http.get(path)

            assert response.headers.get("cache-control") == "no-store", (
                f"{path} may be cached independently of the rest of the set"
            )


@pytest.mark.asyncio
async def test_the_assets_are_named_rather_than_mounted(issuer):
    """
    [claude] `/app.css` and `/app.js` are registered one by one, and a
    directory mount over `static` would be the obvious "tidier" refactor.

    It would also serve whatever anyone later drops in beside the page —
    and the things that get dropped next to a UI are exactly the ones that
    should not be public: a design export, a scratch copy, a `.env`
    somebody was comparing against. This plants such a file and asserts it
    stays unreachable.
    """

    from app.api import app as module

    planted = Path(module.__file__).parent / "static" / "not-for-the-web.txt"
    planted.write_text("PGPASSWORD=hunter2\n")

    try:
        app, _ = build_app(verifier=issuer.verifier())

        async with client(app) as http:
            response = await http.get("/not-for-the-web.txt")

        assert response.status_code == 404, (
            "static/ is being served as a directory; register UI assets by "
            "name instead"
        )
    finally:
        planted.unlink()
