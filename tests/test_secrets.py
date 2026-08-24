"""
[claude] Credentials cannot reach a string by accident.

The five credential fields are `SecretStr`, whose `str()` and `repr()` are
`**********`. That closes the routes by which passwords actually leak in
practice — a traceback rendering local variables, a debug log line
formatting the settings object, an `f"{settings.postgres_password}"` written
in a hurry.

It is not a defence against an attacker. Anything that can call `reveal()`
can read the value, and the process holds it in memory regardless. What it
buys is that leaking one now takes a deliberate call rather than an
incidental format — the difference between a mistake anyone can make and one
you have to mean.

Asserted rather than trusted because the protection is invisible when it
works and equally invisible when someone changes a field back to `str`.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.config import Settings, reveal, settings

SECRET_FIELDS = (
    "postgres_password",
    "crm_postgres_password",
    "postgres_readonly_password",
    "model_api_key",
    "tavily_api_key",
)


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_every_credential_field_is_declared_secret(field):
    """
    A new credential added as a plain `str` is the regression this catches.
    Checked on the annotation, so it holds whether or not the field is set
    in this environment.
    """

    annotation = Settings.model_fields[field].annotation

    assert "SecretStr" in str(annotation), (
        f"{field} is {annotation}; credentials must be SecretStr so they "
        "cannot be formatted into a log line or a traceback"
    )


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_a_credential_does_not_render_itself(field):
    value = getattr(settings, field)

    if value is None:
        pytest.skip(f"{field} is not set in this environment")

    assert str(value) == "**********"
    assert "**********" in repr(value)


# [claude] Short secrets are not searched for, and the reason is a false
# positive this test produced on its first run.
#
# A substring search reported the settings repr as leaking
# `postgres_password` — because on the development fixture that password is
# a short word that also appears inside `postgres_db`, `postgres_user` and
# three unrelated fields. The credential was correctly masked; the test was
# matching the wrong occurrence.
#
# So the scan only runs against secrets long enough to be distinctive.
# Everything shorter is covered by the per-field masking assertions above,
# which do not depend on the value at all.
DISTINCTIVE_LENGTH = 12


def test_the_settings_object_carries_no_credential_in_its_repr():
    """
    The realistic leak. Nobody prints a password on purpose; they print the
    settings object while debugging something else.
    """

    rendered = repr(settings) + str(settings)
    scanned = 0

    for field in SECRET_FIELDS:
        value = getattr(settings, field)

        if value is None:
            continue

        secret = reveal(value) or ""

        if len(secret) < DISTINCTIVE_LENGTH:
            continue

        scanned += 1

        assert secret not in rendered, (
            f"{field} appears verbatim in the settings repr"
        )

    assert scanned, (
        "no secret in this environment was long enough to scan for — the "
        "masking assertions above are what is carrying this file here"
    )


def test_the_masked_form_is_what_appears_instead():
    """
    The other half: not merely that the value is absent, but that something
    stands where it would have been. A field silently dropped from the repr
    would pass the scan above and tell you nothing.
    """

    rendered = repr(settings)

    assert "**********" in rendered


def test_reveal_is_the_only_way_out():
    assert reveal(SecretStr("hunter2")) == "hunter2"
    # Plain strings pass through, so a caller need not know which it holds.
    assert reveal("hunter2") == "hunter2"
    assert reveal(None) is None


def test_an_unset_secret_is_falsy():
    """
    [claude] `if not config.tavily_api_key` is a real branch — it decides
    whether web search is built at all. `SecretStr("")` is falsy, so that
    check keeps working, but it is worth pinning: if it were truthy, an
    unconfigured deployment would build a search tool that fails on use
    instead of reporting the capability as absent.
    """

    assert not SecretStr("")
    assert bool(SecretStr("x"))
