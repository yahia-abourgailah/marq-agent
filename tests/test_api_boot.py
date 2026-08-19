"""
[claude] Booting the real application.

Every other API test injects onto `app.state` what the lifespan would have
built, which is right for them and leaves the boot path itself untested — and
boot failures are the worst kind, because the service is simply not there.

docs/HANDOFF.md: "Test the entry point people actually use. A green suite over
a path nobody uses is not evidence." The first end-to-end run through
`build_supervisor_graph` immediately hit a step ceiling that single-domain
runs never reached; this is the same lesson applied to the HTTP layer.

Marked `integration`: the lifespan opens PostgreSQL pools, runs the
checkpointer's `setup()`, and constructs the model client.
"""

from __future__ import annotations

import httpx
import pytest

from app.api.app import create_app, lifespan
from tests.api_support import TokenIssuer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


@pytest.mark.asyncio
async def test_the_application_boots_and_reports_itself_ready(issuer, monkeypatch):
    """
    The whole startup path, then a readiness check through it.

    [claude] `settings` is patched with real key material rather than dev
    mode, because `TokenVerifier` is constructed during boot and refusing to
    start without a key is one of the behaviours under test.
    """

    monkeypatch.setattr("app.api.app.settings", issuer.settings())

    app = create_app()

    async with lifespan(app):
        # Everything the lifespan is responsible for.
        assert app.state.verifier is not None
        assert app.state.graph is not None
        assert app.state.conversations is not None
        assert app.state.workspace_service is not None
        assert app.state.checkpointer.is_persistent, (
            "booted on an in-memory checkpointer; conversations would not "
            "survive a restart"
        )

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            live = await http.get("/health")
            ready = await http.get("/health/ready")
            unauthorised = await http.post(
                "/v1/chat", json={"message": "how many deals"}
            )

    assert live.status_code == 200

    body = ready.json()

    assert body["database"]["ok"] is True
    assert body["checkpointer"]["ok"] is True
    assert body["model"]["ok"] is True

    # The auth dependency is wired on the real app, not only the test one.
    assert unauthorised.status_code == 401


@pytest.mark.asyncio
async def test_boot_fails_loudly_without_key_material(monkeypatch):
    """
    A deployment missing its signing key must not start.

    [claude] The alternative is a service that boots, serves `/health`
    happily, and 500s on the first real question — a configuration error
    converted into an intermittent one. `TokenVerifier` is built early in
    the lifespan precisely so this happens at boot.
    """

    from app.config import settings as real

    # [claude] All three key sources must be cleared, not two.
    #
    # This test passed while only `jwt_public_key` and `jwt_secret` were
    # nulled — and then broke the moment `.env.development` gained a
    # `JWT_PUBLIC_KEY_PATH`, because the verifier found a key on disk and
    # had nothing to complain about. The test was asserting "no key
    # configured" while describing only the ways of configuring one that
    # existed when it was written.
    broken = real.model_copy(
        update={
            "jwt_algorithm": "RS256",
            "jwt_public_key": None,
            "jwt_public_key_path": None,
            "jwt_secret": None,
            "auth_dev_mode": False,
        }
    )

    monkeypatch.setattr("app.api.app.settings", broken)

    app = create_app()

    with pytest.raises(RuntimeError, match="JWT_PUBLIC_KEY"):
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_the_checkpointer_and_the_index_share_one_pool(issuer, monkeypatch):
    """
    One writable pool, not two.

    The saver and the conversation repository both need writable connections
    to the same database; opening a pool each doubles the connection count
    for no benefit, and the handle would then own a pool the repository is
    still using.
    """

    monkeypatch.setattr("app.api.app.settings", issuer.settings())

    app = create_app()

    async with lifespan(app):
        assert app.state.checkpointer.pool is app.state.state_pool
        assert app.state.conversations.pool is app.state.state_pool
        # Borrowed, so closing the handle must not close the shared pool.
        assert app.state.checkpointer.owns_pool is False
