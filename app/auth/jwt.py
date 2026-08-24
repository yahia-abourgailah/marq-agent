"""
[claude] Bearer token verification.

The front end authenticates its own users and presents a signed JWT here. The
employee id is read from a claim, so it is asserted by whoever holds the
signing key rather than by the caller — which is the whole point, because that
id becomes `app.requester_id` in PostgreSQL and decides which rows the
row-level security policy in `migrations/002_row_level_security.sql` will
return.
"""

from __future__ import annotations

from pathlib import Path

import jwt
from jwt import InvalidSignatureError, PyJWTError

from app.auth.principal import Principal
from app.config import APP_ENV, Settings, reveal, settings

_PEM_BOUNDARY = "-----BEGIN"


def _split_pem(material: str) -> list[str]:
    """
    Split concatenated PEM blocks into individual keys.

    [claude] Text before the first boundary is discarded rather than
    treated as a key — PEM files routinely carry a comment header, and
    passing one to a verifier produces an error that quotes the key
    material.
    """

    text = (material or "").strip()

    if not text:
        return []

    if _PEM_BOUNDARY not in text:
        # Not PEM at all. Hand it over unchanged and let the verifier
        # complain about it, rather than silently returning nothing.
        return [text]

    parts = text.split(_PEM_BOUNDARY)

    return [
        f"{_PEM_BOUNDARY}{part.rstrip()}"
        for part in parts[1:]
        if part.strip()
    ]


class AuthError(Exception):
    """
    Authentication failed.

    [claude] A typed outcome rather than a leaked library error, following
    the same rule as design decision 6 in docs/HANDOFF.md: only the type
    crosses the boundary. PyJWT's messages are safe enough on their own, but
    the ones from the key material are not — a malformed key raises errors
    that quote it.

    `reason` is for the log; the HTTP layer sends a fixed string.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TokenVerifier:
    """
    Verifies bearer tokens against the configured issuer.

    Constructed once at startup rather than per request, so a deployment
    missing its key material fails immediately and loudly instead of at the
    first user's first question.
    """

    def __init__(self, config: Settings | None = None) -> None:
        self.settings = config or settings

        self.algorithm = self.settings.jwt_algorithm
        self.dev_mode = self.settings.auth_dev_mode

        # [claude] A flag that turns off authentication defends itself
        # rather than trusting deployment to get it right. Left on by
        # accident in production it would accept an identity from an
        # unsigned header, which is every employee's data to any caller.
        if self.dev_mode and APP_ENV == "production":
            raise RuntimeError(
                "auth_dev_mode cannot be enabled when APP_ENV=production. "
                "It accepts an unsigned identity header and must never "
                "reach a deployment holding real data."
            )

        # HS* verifies with the shared secret; everything else with a
        # public key.
        symmetric = self.algorithm.upper().startswith("HS")

        # [claude] Keys, plural — see `verify`. During a rotation two are
        # valid at once; the rest of the time this is a list of one.
        self.keys = (
            self._secrets() if symmetric else self._public_keys()
        )

        # Kept so existing callers and tests reading `.key` still work; it
        # is the key a *new* token is expected to be signed with.
        self.key = self.keys[0] if self.keys else None

        if not self.key and not self.dev_mode:
            expected = "JWT_SECRET" if symmetric else "JWT_PUBLIC_KEY"

            raise RuntimeError(
                f"{expected} is required to verify {self.algorithm} tokens. "
                f"Set it, or set AUTH_DEV_MODE=true for local development "
                f"without a token issuer."
            )

    def _secrets(self) -> list[str]:
        """Shared secrets for HS*: the current one, then any retired one."""

        return [
            reveal(secret)
            for secret in (
                self.settings.jwt_secret,
                self.settings.jwt_secret_previous,
            )
            if reveal(secret)
        ]

    def _public_keys(self) -> list[str]:
        """
        Every public key a token may legitimately be signed with.

        [claude] A list, so a signing key can be rotated without an outage.

        With one key there is no safe moment to change it: the instant the
        new key is configured, every token already in a browser becomes
        invalid, and every user is signed out mid-question. Accepting the
        outgoing key alongside the incoming one turns that cliff into a
        window — publish the new key, wait out the old token TTL, then
        remove the old key.

        The format is a PEM bundle: several `-----BEGIN PUBLIC KEY-----`
        blocks concatenated, in either `JWT_PUBLIC_KEY` or the file at
        `JWT_PUBLIC_KEY_PATH`. That is a format people already have tooling
        for, and it needs no new configuration field.

        No `kid` handling, deliberately. Selecting by key id needs a
        published id-to-key mapping — JWKS — and without one, `kid` is a
        hint from the token about which key to trust, which is not an input
        worth honouring. Trying each key is equivalent while the algorithm
        is pinned, and the list is two long during a rotation and one
        otherwise.

        The literal still wins over the file when both are configured, and
        a configured path that does not exist still raises rather than
        degrading to "no key".
        """

        if self.settings.jwt_public_key:
            return _split_pem(self.settings.jwt_public_key)

        if not self.settings.jwt_public_key_path:
            return []

        path = Path(self.settings.jwt_public_key_path).expanduser()

        if not path.is_file():
            raise RuntimeError(
                f"JWT_PUBLIC_KEY_PATH points at {str(path)!r}, which does "
                f"not exist. Generate a development key with:\\n"
                f"    python scripts/dev_token.py init"
            )

        return _split_pem(path.read_text())

    def verify(self, token: str) -> Principal:
        """
        Verify one token and return the caller it identifies.

        [claude] Tries every configured key, and only for a *signature*
        failure.

        That distinction is the whole design. An expired token, a wrong
        audience or a missing claim will fail identically against every key,
        so retrying them is wasted work that also replaces the real reason
        with whatever the last key happened to say — and `reason` is what
        goes in the log a support request is answered from.

        Trying several keys is safe because the algorithm is pinned to the
        one configured value. Without that pin this would be a way to widen
        the classic confusion attack, since more keys means more chances for
        a token to name one it can abuse.
        """

        if not self.key:
            # Only reachable in dev mode, where get_principal never calls
            # this. Explicit so a future caller cannot get an unverified
            # Principal by taking a different path here.
            raise AuthError("no verification key configured")

        last_error: Exception | None = None

        for key in self.keys:
            try:
                return self._decode(token, key)
            except InvalidSignatureError as exc:
                # Signed by a different key. Try the next one — this is the
                # rotation window, where two are legitimately in use.
                last_error = exc
                continue
            except PyJWTError as exc:
                # Expired, wrong audience, missing claim: every key will
                # say the same thing, so report this one.
                raise AuthError(type(exc).__name__) from exc

        raise AuthError(
            type(last_error).__name__
            if last_error
            else "InvalidSignatureError"
        )

    def _decode(self, token: str, key: str) -> Principal:
        """Verify against exactly one key."""

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[self.algorithm],
                # [claude] Pinned to the one configured algorithm. Accepting
                # a list the token can choose from is the classic JWT
                # confusion attack: a token declaring `alg: HS256` verified
                # against an RSA *public* key succeeds, because the public
                # key is not secret and becomes the HMAC secret.
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                options={
                    "require": ["exp", self.settings.jwt_subject_claim],
                    "verify_exp": True,
                    "verify_signature": True,
                    # Only verified when configured — PyJWT raises if asked
                    # to check an audience that was never set.
                    "verify_aud": self.settings.jwt_audience is not None,
                    "verify_iss": self.settings.jwt_issuer is not None,
                },
            )
        except PyJWTError:
            # [claude] Raised on, not converted here.
            #
            # `verify` above needs to see the *type* to decide whether
            # another key is worth trying — a signature failure means "not
            # this key", anything else means "not any key". Converting to
            # AuthError at this depth made every failure look alike, so the
            # rotation loop never advanced past the first key and a token
            # signed with the outgoing one was rejected. `verify` does the
            # conversion, so the type still never crosses the boundary.
            raise

        subject = claims.get(self.settings.jwt_subject_claim)

        # [claude] `require` above already rejects a missing claim, but not
        # an empty or non-string one. An empty subject would hash to a
        # perfectly valid workspace id and publish an empty requester id,
        # which reads to a policy as "nobody" — silently matching nothing
        # while looking like it worked.
        if not isinstance(subject, str) or not subject.strip():
            raise AuthError(
                f"claim {self.settings.jwt_subject_claim!r} is missing or empty"
            )

        scopes = claims.get("scope") or claims.get("scopes") or ()

        if isinstance(scopes, str):
            scopes = tuple(scopes.split())
        else:
            scopes = tuple(str(scope) for scope in scopes)

        return Principal(subject=subject.strip(), scopes=scopes)


__all__ = ["AuthError", "TokenVerifier"]
