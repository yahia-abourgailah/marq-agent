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
from jwt import PyJWTError

from app.auth.principal import Principal
from app.config import APP_ENV, Settings, settings


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

        self.key = (
            self.settings.jwt_secret
            if symmetric
            else self._public_key()
        )

        if not self.key and not self.dev_mode:
            expected = "JWT_SECRET" if symmetric else "JWT_PUBLIC_KEY"

            raise RuntimeError(
                f"{expected} is required to verify {self.algorithm} tokens. "
                f"Set it, or set AUTH_DEV_MODE=true for local development "
                f"without a token issuer."
            )

    def _public_key(self) -> str | None:
        """
        The verification key: a literal, or the contents of a file.

        [claude] The literal wins when both are configured, so an explicit
        `JWT_PUBLIC_KEY` is never silently overridden by a stale file left
        on disk.

        A configured path that does not exist raises rather than falling
        back to None. Falling back would mean a typo in the path produced
        "no key configured", which — in dev mode — degrades to accepting an
        unsigned header instead of failing.
        """

        if self.settings.jwt_public_key:
            return self.settings.jwt_public_key

        if not self.settings.jwt_public_key_path:
            return None

        path = Path(self.settings.jwt_public_key_path).expanduser()

        if not path.is_file():
            raise RuntimeError(
                f"JWT_PUBLIC_KEY_PATH points at {str(path)!r}, which does "
                f"not exist. Generate a development key with:\n"
                f"    python scripts/dev_token.py init"
            )

        return path.read_text()

    def verify(self, token: str) -> Principal:
        """Verify one token and return the caller it identifies."""

        if not self.key:
            # Only reachable in dev mode, where get_principal never calls
            # this. Explicit so a future caller cannot get an unverified
            # Principal by taking a different path here.
            raise AuthError("no verification key configured")

        try:
            claims = jwt.decode(
                token,
                self.key,
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
        except PyJWTError as exc:
            # Type only, never the exception text — see AuthError.
            raise AuthError(type(exc).__name__) from exc

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
