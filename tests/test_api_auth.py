"""
[claude] The authentication boundary.

This is the file that matters most in the API suite. Everything downstream —
the guard, the read-only role, the row-level security policy in
migrations/002 — assumes `requester_id` is an identity somebody proved. If
that assumption is wrong, none of the layers below it are doing what their
docstrings say.

So these tests are written as attacks rather than as usage: no token, a
forged token, an expired one, one for a different audience, one that picks
its own algorithm, and an identity smuggled through the request body.
"""

from __future__ import annotations

import time

import jwt
import pytest

from app.auth.jwt import AuthError, TokenVerifier
from app.auth.principal import Principal, workspace_id_for
from tests.api_support import (
    AUDIENCE,
    ISSUER,
    StubGraph,
    TokenIssuer,
    build_app,
    client,
)


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


@pytest.fixture
def app_and_parts(issuer):
    return build_app(verifier=issuer.verifier())


# ============================================================
# Getting in without a valid token
# ============================================================


PROTECTED = [
    ("POST", "/v1/chat", {"message": "how many deals"}),
    ("POST", "/v1/chat/stream", {"message": "how many deals"}),
    ("GET", "/v1/threads", None),
    ("GET", "/v1/threads/abc", None),
    ("DELETE", "/v1/threads/abc", None),
    ("GET", "/v1/workspace/files", None),
    ("DELETE", "/v1/workspace/files/abc", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", PROTECTED)
async def test_no_token_is_rejected_everywhere(app_and_parts, method, path, body):
    """
    Every route that touches data requires a token.

    Parametrised over the whole surface rather than spot-checked, because
    the failure mode is one route that forgot the dependency — and that
    route is invisible in a test suite that only covers the others.
    """

    app, _ = app_and_parts

    async with client(app) as http:
        response = await http.request(method, path, json=body)

    assert response.status_code == 401, path
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "token abc.def.ghi",
        "bearer",
    ],
)
async def test_malformed_authorization_headers_are_rejected(app_and_parts, header):
    app, _ = app_and_parts

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi"},
            headers={"Authorization": header},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_token_signed_by_someone_else_is_rejected(app_and_parts):
    """A valid-looking token from a keypair we do not trust."""

    app, _ = app_and_parts
    attacker = TokenIssuer()

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi"},
            headers=attacker.auth("employee-1"),
        )

    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs,why",
    [
        ({"expires_in": -60}, "expired"),
        ({"issuer": "https://evil.test"}, "wrong issuer"),
        ({"audience": "some-other-service"}, "wrong audience"),
    ],
)
async def test_tokens_failing_a_standard_claim_are_rejected(
    app_and_parts, issuer, kwargs, why
):
    app, _ = app_and_parts

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi"},
            headers=issuer.auth("employee-1", **kwargs),
        )

    assert response.status_code == 401, why


@pytest.mark.asyncio
async def test_an_unsigned_token_is_rejected(app_and_parts, issuer):
    """
    The `alg: none` token.

    [claude] Worth its own test rather than trusting the library. This is the
    canonical JWT bypass, and a verifier that passes an `algorithms` list
    including "none" — or that lets the token choose — accepts an identity
    anyone can mint with a text editor.
    """

    app, _ = app_and_parts

    import base64
    import json

    def part(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = part(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = part(
        json.dumps(
            {
                "sub": "admin",
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": int(time.time()) + 300,
            }
        ).encode()
    )
    unsigned = (header + b"." + payload + b".").decode()

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi"},
            headers={"Authorization": f"Bearer {unsigned}"},
        )

    assert response.status_code == 401


def test_the_verifier_pins_one_algorithm(issuer):
    """
    A token declaring HS256 is not verified against the RSA public key.

    The algorithm-confusion attack: the public key is not secret, so if the
    token may choose its own algorithm, an attacker HMACs with the published
    key and the signature checks out. Asserted at the verifier because the
    forged token cannot be built with PyJWT's encoder at all.
    """

    verifier = issuer.verifier()

    assert verifier.algorithm == "RS256"

    import base64
    import hashlib
    import hmac
    import json

    def part(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = part(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = part(
        json.dumps(
            {
                "sub": "admin",
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": int(time.time()) + 300,
            }
        ).encode()
    )
    signing_input = header + b"." + payload
    signature = part(
        hmac.new(
            issuer.public_pem.encode(), signing_input, hashlib.sha256
        ).digest()
    )

    from app.auth.jwt import AuthError

    with pytest.raises(AuthError):
        verifier.verify((signing_input + b"." + signature).decode())


@pytest.mark.asyncio
async def test_a_token_with_an_empty_subject_is_rejected(app_and_parts, issuer):
    """
    An empty subject would hash to a perfectly valid workspace id and
    publish an empty `app.requester_id`, which an RLS policy reads as
    "nobody" — silently matching no rows while looking like it worked.
    """

    app, _ = app_and_parts

    token = jwt.encode(
        {
            "sub": "   ",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": int(time.time()) + 300,
        },
        issuer.private_pem,
        algorithm="RS256",
    )

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401


# ============================================================
# The identity that reaches the graph
# ============================================================


@pytest.mark.asyncio
async def test_identity_reaching_the_graph_comes_from_the_token(issuer):
    """
    The property the whole layer exists for.

    `requester_id` and `workspace_id` are read off the verified token and
    handed to the graph. Asserted on what the graph was *passed*, not on the
    answer — the answer would look identical if the ids were wrong.
    """

    graph = StubGraph()
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "how many deals"},
            headers=issuer.auth("employee-42"),
        )

    assert response.status_code == 200

    state = graph.calls[-1]["state"]

    assert state["requester_id"] == "employee-42"
    assert state["workspace_id"] == workspace_id_for("employee-42")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["workspace_id", "requester_id", "subject"])
async def test_identity_cannot_be_supplied_in_the_request_body(issuer, field):
    """
    Sending an identity field is a 422, not a silently ignored value.

    [claude] Rejecting rather than ignoring is the deliberate part. An
    ignored field returns 200 with an answer computed from the caller's own
    identity, so a front end that sends `workspace_id` concludes it works —
    and the mistake surfaces much later, as a user seeing data they should
    not, or as a feature that never worked.
    """

    graph = StubGraph()
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi", field: "somebody-else"},
            headers=issuer.auth("employee-1"),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert graph.calls == []


@pytest.mark.asyncio
async def test_two_employees_get_different_workspaces(issuer):
    """A workspace is derived from the subject, so it cannot be shared."""

    graph = StubGraph()
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        for subject in ("alice@example.com", "bob@example.com"):
            await http.post(
                "/v1/chat",
                json={"message": "hi"},
                headers=issuer.auth(subject),
            )

    alice, bob = (call["state"]["workspace_id"] for call in graph.calls)

    assert alice != bob


def test_a_derived_workspace_id_is_always_a_valid_slug():
    """
    `WorkspaceStore` rejects ids outside `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`.

    Realistic subjects contain `@`, `.` and `|`, none of which are in that
    pattern — so using the subject directly would fail for real tokens, and
    would put an email address on disk as a directory name.
    """

    from app.workspace.store import validate_slug

    for subject in (
        "user@example.com",
        "auth0|abc123",
        "550e8400-e29b-41d4-a716-446655440000",
        "employee 42",
        "../../etc/passwd",
        "42",
        "دائرة",
    ):
        validate_slug(workspace_id_for(subject), "workspace id")


def test_dev_mode_cannot_be_enabled_in_production(issuer):
    """
    A flag that turns off authentication defends itself.

    Left on by accident in production it accepts an identity from an
    unsigned header, which is every employee's data to any caller. The
    verifier refuses to construct, so the process does not start.
    """

    import app.auth.jwt as module

    original = module.APP_ENV
    module.APP_ENV = "production"

    try:
        with pytest.raises(RuntimeError, match="auth_dev_mode"):
            TokenVerifier(issuer.settings(auth_dev_mode=True))
    finally:
        module.APP_ENV = original


@pytest.mark.asyncio
async def test_debug_subject_header_is_ignored_when_not_in_dev_mode(issuer):
    """The unsigned identity header does nothing unless dev mode is on."""

    app, _ = build_app(verifier=issuer.verifier())

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi"},
            headers={"X-Debug-Subject": "admin"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_bearer_token_wins_over_the_debug_header_in_dev_mode(issuer):
    """
    In dev mode the header is a fallback, never an override.

    Otherwise a caller holding a valid token for themselves could present it
    alongside a header naming somebody else and be believed.
    """

    graph = StubGraph()
    app, _ = build_app(
        verifier=issuer.verifier(auth_dev_mode=True), graph=graph
    )

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi"},
            headers={
                **issuer.auth("real-employee"),
                "X-Debug-Subject": "admin",
            },
        )

    assert response.status_code == 200
    assert graph.calls[-1]["state"]["requester_id"] == "real-employee"


def test_thread_keys_are_namespaced_per_subject():
    """
    Two employees using the same thread id must not collide.

    Namespacing rather than checking-then-using means there is no path that
    forgets the check, because there is no unnamespaced key to use.
    """

    alice = Principal("alice@example.com")
    bob = Principal("bob@example.com")

    assert alice.thread_key("today") != bob.thread_key("today")
    assert alice.thread_key("today") == alice.thread_key("today")
    assert "alice@example.com" not in alice.thread_key("today")


# ============================================================
# Where the verification key comes from
# ============================================================


def test_the_public_key_can_be_read_from_a_file(issuer, tmp_path):
    """
    `JWT_PUBLIC_KEY_PATH` is how a key arrives in production — mounted into
    the container as a file rather than wedged into an env var.
    """

    key_file = tmp_path / "public.pem"
    key_file.write_text(issuer.public_pem)

    verifier = TokenVerifier(
        issuer.settings(jwt_public_key=None, jwt_public_key_path=str(key_file))
    )

    principal = verifier.verify(issuer.token("employee-9"))

    assert principal.subject == "employee-9"


def test_a_key_path_that_does_not_exist_raises_rather_than_degrading(issuer):
    """
    [claude] The important half.

    Falling back to "no key configured" on a typo'd path would be a silent
    downgrade: in dev mode that state accepts an identity from an unsigned
    header, so a misspelled filename would turn signature verification off
    rather than fail. It raises at construction instead, so the process does
    not start.
    """

    with pytest.raises(RuntimeError, match="does not exist"):
        TokenVerifier(
            issuer.settings(
                jwt_public_key=None,
                jwt_public_key_path="/nonexistent/nowhere/public.pem",
            )
        )


def test_an_explicit_key_wins_over_a_stale_file(issuer, tmp_path):
    """
    Both configured means the literal is authoritative, so an explicit key
    is never silently overridden by a file left on disk from an earlier
    deployment.
    """

    other = TokenIssuer()

    stale = tmp_path / "stale.pem"
    stale.write_text(other.public_pem)

    verifier = TokenVerifier(
        issuer.settings(
            jwt_public_key=issuer.public_pem,
            jwt_public_key_path=str(stale),
        )
    )

    # Signed by `issuer`, whose key is the literal — accepted.
    assert verifier.verify(issuer.token("employee-1")).subject == "employee-1"

    # Signed by the key in the stale file — rejected.
    with pytest.raises(AuthError):
        verifier.verify(other.token("attacker"))


def test_verify_refuses_outright_when_no_key_is_configured(issuer):
    """
    Reachable only in dev mode, where `get_principal` never calls it. Kept
    explicit so a future caller cannot obtain an unverified Principal by
    taking a different route into this method.
    """

    verifier = TokenVerifier(
        issuer.settings(
            jwt_public_key=None, jwt_public_key_path=None, auth_dev_mode=True
        )
    )

    assert verifier.key is None

    with pytest.raises(AuthError):
        verifier.verify(issuer.token())


# ============================================================
# Development mode, the accepting path
# ============================================================


@pytest.mark.asyncio
async def test_dev_mode_accepts_the_debug_subject_header(issuer):
    """
    The rejection paths are covered above; this is the branch that actually
    lets someone in, and it should be exercised rather than assumed.
    """

    graph = StubGraph()
    app, _ = build_app(
        verifier=issuer.verifier(auth_dev_mode=True), graph=graph
    )

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "How many deals?"},
            headers={"X-Debug-Subject": "dev@example.com"},
        )

    assert response.status_code == 200
    assert graph.calls[-1]["state"]["requester_id"] == "dev@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["", "   "])
async def test_dev_mode_still_refuses_an_empty_debug_subject(issuer, header):
    """
    An empty subject would hash to a valid workspace id and publish an empty
    requester id, which a policy reads as "nobody" — silently matching
    nothing while looking like it worked.
    """

    app, _ = build_app(verifier=issuer.verifier(auth_dev_mode=True))

    async with client(app) as http:
        response = await http.post(
            "/v1/chat",
            json={"message": "hi"},
            headers={"X-Debug-Subject": header},
        )

    assert response.status_code == 401
