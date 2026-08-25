"""
[claude] The console signing itself in, and the two gates that stop that
reaching production.

`DEV_UI_TOKEN` lets a developer open `/` and have it work, instead of
minting a token and pasting it. That convenience is a bearer token served
to anyone who can load the page, so it is gated twice rather than
documented once:

    the route is not registered when APP_ENV=production
    create_app refuses outright if the variable is set there

Two gates because the failure is silent. A `.env.production` that inherited
the variable from a copied development file would hand out a working token
and nothing about the deployment would look wrong — which is the same shape
as `auth_dev_mode`, and it is guarded the same way.
"""

from __future__ import annotations

import json

import pytest

from tests.api_support import TokenIssuer, build_app, client


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


@pytest.mark.asyncio
async def test_the_console_is_served_a_token_when_one_is_configured(
    issuer, monkeypatch
):
    from app.api import app as module

    monkeypatch.setattr(
        module.settings, "dev_ui_token", "a-development-token", raising=False
    )

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/app-config.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "a-development-token" in response.text
    # Never cached — it carries a credential and the credential rotates.
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_no_token_configured_serves_null_rather_than_nothing(
    issuer, monkeypatch
):
    """
    The script is still served, setting `null`. A 404 would leave
    `window.MARQ_DEV_TOKEN` undefined, which works — but a defined `null`
    says "asked and answered" rather than "the request failed", and the
    console distinguishes those nowhere else either.
    """

    from app.api import app as module

    monkeypatch.setattr(module.settings, "dev_ui_token", None, raising=False)

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.get("/app-config.js")

    assert response.status_code == 200
    assert "null" in response.text


@pytest.mark.asyncio
async def test_a_token_containing_quotes_cannot_become_script(
    issuer, monkeypatch
):
    """
    [claude] JSON-encoded rather than interpolated.

    A value carrying a quote or a backslash would otherwise terminate the
    string literal and everything after it would be executed. The token is
    ours rather than an attacker's, but a template that is only safe
    because of what is currently in it is not safe.
    """

    from app.api import app as module

    hostile = '";window.stolen=1;//'
    monkeypatch.setattr(
        module.settings, "dev_ui_token", hostile, raising=False
    )

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        body = (await http.get("/app-config.js")).text

    # The whole value survives as one JSON string.
    payload = body.split("=", 1)[1].strip().rstrip(";").strip()

    assert json.loads(payload) == hostile
    assert "window.stolen" not in body.replace(json.dumps(hostile), "")


def test_the_route_is_absent_in_production(monkeypatch):
    from app.api import app as module

    monkeypatch.setattr(module, "APP_ENV", "production")
    monkeypatch.setattr(module.settings, "dev_ui_token", None, raising=False)

    api = module.create_app()
    paths = {getattr(r, "path", None) for r in api.routes}

    assert "/app-config.js" not in paths
    # The console itself is unaffected; only the token handout goes.
    assert "/" in paths


def test_a_configured_token_refuses_to_boot_in_production(monkeypatch):
    """
    The gate that matters. Not registering the route protects the token
    only if nobody re-registers it; refusing to start protects it from a
    misconfigured environment file, which is how it would actually happen.
    """

    from app.api import app as module

    monkeypatch.setattr(module, "APP_ENV", "production")
    monkeypatch.setattr(
        module.settings, "dev_ui_token", "leaked-from-a-copied-env", raising=False
    )

    with pytest.raises(RuntimeError, match="DEV_UI_TOKEN"):
        module.create_app()


def test_the_page_asks_for_the_config_before_its_behaviour(issuer):
    """
    Load order, asserted because it is invisible when correct: `app.js`
    reads `window.MARQ_DEV_TOKEN` at the top level, so a config script
    loaded after it would set a variable nobody reads.
    """

    from pathlib import Path

    from app.api import app as module

    page = (
        Path(module.__file__).parent / "static" / "index.html"
    ).read_text()

    assert page.index("/app-config.js") < page.index("/app.js")
