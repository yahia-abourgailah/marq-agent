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


# ============================================================
# Which token wins
# ============================================================
#
# [claude] These read the shipped script rather than executing it, because
# there is no JS test runner here and adding one for four assertions is a
# worse trade than checking the contract is present. The behaviour itself
# was verified in a browser: a stale token planted in localStorage was
# replaced by the configured one on reload, a pasted override survived a
# reload, and clearing the field handed control back to the environment.


def ui_script() -> str:
    from pathlib import Path

    from app.api import app as module

    return (Path(module.__file__).parent / "static" / "app.js").read_text()


def test_the_configured_token_wins_over_a_stored_one():
    """
    The first version had a stored token win, so a browser used before this
    existed kept its old short-lived token and silently ignored the
    configured one — the precise problem the setting was added to remove.
    """

    script = ui_script()

    assert "function adoptToken()" in script
    assert "served && !manualOverride()" in script


def test_an_explicit_override_is_recorded_separately():
    """
    A pasted identity still survives a reload, but only because it is
    marked as deliberate — not because whatever happens to be in storage
    outranks configuration.
    """

    script = ui_script()

    assert 'MANUAL_KEY = "marq_token_manual"' in script
    assert "localStorage.setItem(MANUAL_KEY" in script


def test_clearing_the_field_returns_control_to_the_environment():
    """
    Otherwise clearing it leaves the console signed out beside a perfectly
    good configured token, which is a dead end with no way back except
    knowing to paste again.
    """

    script = ui_script()

    assert "localStorage.removeItem(MANUAL_KEY)" in script


def test_the_panel_names_where_the_identity_came_from():
    """
    "Where is this identity coming from" is the first question when the
    console is signed in as somebody unexpected, and with DEV_UI_TOKEN
    there are now two possible answers.
    """

    script = ui_script()

    assert "tokenFromEnvironment" in script
    assert "DEV_UI_TOKEN" in script


def test_the_identity_block_is_read_only_when_the_environment_manages_it():
    """
    [claude] A password field labelled "paste a bearer token" is a task,
    and a task already done reads as one still outstanding. It also invites
    a user of an internal tool to think credentials are their problem,
    which is the opposite of what configuring DEV_UI_TOKEN achieved.

    Verified in a browser: with a configured token the row has no chevron,
    no pointer cursor, is not in the tab order, and clicking it does
    nothing; without one, all four come back.
    """

    script = ui_script()

    assert "function applyIdentityAffordance()" in script
    assert 'btn.classList.toggle("managed", managed)' in script
    # Refused at the click, not only hidden in CSS.
    assert "if (tokenFromEnvironment) return;" in script


def test_hiding_the_control_does_not_remove_the_capability():
    """
    A developer testing a second identity still needs a way in. The palette
    keeps one, which is why the drawer is hidden rather than deleted.
    """

    script = ui_script()

    assert "access token" in script
    assert "toggleToken(true)" in script
