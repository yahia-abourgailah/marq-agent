"""
[claude] Rotating a signing key without signing everyone out.

With one verification key there is no safe moment to change it. The instant
the new key is configured, every token already in a browser becomes invalid
and every user is signed out mid-question — so in practice the key never
gets rotated, which is the actual failure: a credential nobody can replace
under normal conditions is one that stays in place after it should have gone.

Accepting the outgoing key alongside the incoming one turns that cliff into
a window: publish the new key, wait out the longest token TTL, remove the
old. These assert both ends of that window and, just as importantly, that
the window does not weaken anything while it is open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from app.auth.jwt import AuthError, TokenVerifier, _split_pem
from tests.api_support import AUDIENCE, ISSUER, TokenIssuer, build_app, client


@pytest.fixture(scope="module")
def old():
    return TokenIssuer()


@pytest.fixture(scope="module")
def new():
    return TokenIssuer()


def bundle(*issuers) -> str:
    """The PEM bundle a rotation publishes."""

    return "\n".join(i.public_pem for i in issuers)


# ============================================================
# The window
# ============================================================


def test_a_token_from_the_outgoing_key_still_verifies(old, new):
    """
    The property the whole feature exists for. A browser holding a token
    signed minutes before the rotation keeps working.
    """

    verifier = TokenVerifier(new.settings(jwt_public_key=bundle(new, old)))

    principal = verifier.verify(old.token(subject="employee-7"))

    assert principal.subject == "employee-7"


def test_a_token_from_the_incoming_key_verifies(old, new):
    verifier = TokenVerifier(new.settings(jwt_public_key=bundle(new, old)))

    assert verifier.verify(new.token(subject="employee-7")).subject == "employee-7"


def test_closing_the_window_rejects_the_old_key(old, new):
    """
    The other end. Once the old key is removed from the bundle, tokens
    signed with it stop working — which is the point of rotating.
    """

    verifier = TokenVerifier(new.settings(jwt_public_key=new.public_pem))

    with pytest.raises(AuthError):
        verifier.verify(old.token())


def test_a_third_party_key_is_never_accepted(old, new):
    """A window for two keys is not a window for any key."""

    stranger = TokenIssuer()
    verifier = TokenVerifier(new.settings(jwt_public_key=bundle(new, old)))

    with pytest.raises(AuthError):
        verifier.verify(stranger.token())


# ============================================================
# What must not get weaker while the window is open
# ============================================================


def test_expiry_is_still_enforced_against_every_key(old, new):
    verifier = TokenVerifier(new.settings(jwt_public_key=bundle(new, old)))

    with pytest.raises(AuthError):
        verifier.verify(old.token(expires_in=-60))


def test_audience_is_still_enforced(old, new):
    verifier = TokenVerifier(new.settings(jwt_public_key=bundle(new, old)))

    with pytest.raises(AuthError):
        verifier.verify(old.token(audience="some-other-service"))


def test_a_non_signature_failure_reports_its_real_reason(old, new):
    """
    [claude] Why `verify` only falls through on `InvalidSignatureError`.

    An expired token fails identically against every key. Retrying it would
    be wasted work, and worse: the reported reason would become whatever
    the last key happened to raise, replacing "expired" with a signature
    complaint in the log a support request is answered from.
    """

    verifier = TokenVerifier(new.settings(jwt_public_key=bundle(new, old)))

    with pytest.raises(AuthError) as caught:
        verifier.verify(new.token(expires_in=-60))

    assert caught.value.reason == "ExpiredSignatureError"


def test_the_algorithm_is_still_pinned_with_several_keys(old, new):
    """
    More keys means more chances for a token to name an algorithm it can
    abuse — a public key doubling as an HMAC secret is the classic
    confusion attack. The pin is what makes trying several keys safe, so it
    is asserted here rather than only where it was introduced.
    """

    verifier = TokenVerifier(new.settings(jwt_public_key=bundle(new, old)))

    # [claude] Forged by hand rather than with PyJWT.
    #
    # PyJWT refuses to *encode* with an asymmetric key as an HMAC secret,
    # which is a good defence and the wrong thing to lean on here — an
    # attacker is not using PyJWT. This builds the token the way they
    # would: the public key, which is not secret, used directly as the HMAC
    # secret, with the header declaring HS256.
    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(
        json.dumps(
            {
                "sub": "attacker",
                "aud": AUDIENCE,
                "iss": ISSUER,
                "exp": 9_999_999_999,
            }
        ).encode()
    )
    signing_input = header + b"." + payload
    signature = b64(
        hmac.new(
            new.public_pem.encode(), signing_input, hashlib.sha256
        ).digest()
    )
    forged = (signing_input + b"." + signature).decode()

    with pytest.raises(AuthError):
        verifier.verify(forged)


# ============================================================
# Parsing the bundle
# ============================================================


def test_a_bundle_splits_into_its_keys(old, new):
    keys = _split_pem(bundle(new, old))

    assert len(keys) == 2
    assert all(k.startswith("-----BEGIN") for k in keys)


def test_a_single_key_is_a_bundle_of_one(new):
    assert len(_split_pem(new.public_pem)) == 1


def test_a_comment_header_is_not_mistaken_for_a_key(new):
    """
    PEM files routinely carry a comment before the first boundary. Passing
    one to a verifier produces an error quoting the key material.
    """

    keys = _split_pem(f"# issued 2026-08-24 by ops\n{new.public_pem}")

    assert len(keys) == 1
    assert keys[0].startswith("-----BEGIN")


def test_empty_material_is_no_keys():
    assert _split_pem("") == []
    assert _split_pem("   \n ") == []


# ============================================================
# Through HTTP
# ============================================================


@pytest.mark.asyncio
async def test_both_keys_work_through_the_api_during_a_rotation(old, new):
    verifier = TokenVerifier(new.settings(jwt_public_key=bundle(new, old)))
    app, _ = build_app(verifier=verifier)

    async with client(app) as http:
        for issuer in (new, old):
            response = await http.get(
                "/v1/threads",
                headers={"Authorization": f"Bearer {issuer.token()}"},
            )

            assert response.status_code == 200
