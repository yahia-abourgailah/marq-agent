#!/usr/bin/env python3
"""
[claude] Mint development bearer tokens.

Why this exists
---------------
The API verifies real JWTs, and there is no token issuer yet — the company
website will be one eventually. Without this the only way in is
`AUTH_DEV_MODE=true` plus an unsigned `X-Debug-Subject` header, which is a
*bypass*: it never runs the signature, expiry, issuer or audience checks. Test
only that and the first genuine token is the first time the verification code
has ever executed.

So this stands in for the issuer. It generates a keypair once, keeps the
private half on disk, and signs tokens with it. The API verifies them exactly
as it will verify the real thing.

    python scripts/dev_token.py init                       # once
    python scripts/dev_token.py mint                       # a token, 24h
    python scripts/dev_token.py mint --subject bob@x.com   # as someone else
    python scripts/dev_token.py mint --expires 60          # short-lived
    python scripts/dev_token.py header                     # 'Authorization: ...'
    python scripts/dev_token.py check                      # diagnose a rejection

The private key lives under `var/`, which is gitignored — and this refuses to
run against a production environment, because a locally-minted identity is
every employee's data to whoever holds the file.

Named `dev_token.py`, not `token.py`
------------------------------------
A script called `token.py` shadows the standard library's `token` module. The
script's own directory goes on `sys.path` ahead of the stdlib when it runs, so
`tokenize` — imported by `inspect`, imported by `dataclasses`, imported by
`cryptography` — picked up this file instead and died with a circular-import
`AttributeError` naming `inspect`, which points nowhere near the cause. The
same reason `app/logging_config.py` is not called `logging.py`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from app.config import APP_ENV, settings  # noqa: E402

KEY_DIR = Path("var/dev-jwt")
PRIVATE_KEY = KEY_DIR / "private.pem"
PUBLIC_KEY = KEY_DIR / "public.pem"

DEFAULT_SUBJECT = "alice@example.com"
DEFAULT_ISSUER = "https://dev.marq.local"
DEFAULT_AUDIENCE = "marq-agent"


def refuse_in_production() -> None:
    """
    [claude] A locally-minted token is an identity anyone with this file can
    assume. That is fine against a fixture database and unacceptable against
    real CRM data, so the script defends itself rather than relying on
    whoever runs it to notice.
    """

    if APP_ENV == "production":
        raise SystemExit(
            "Refusing to run with APP_ENV=production.\n"
            "This mints identities with a key kept on disk; production "
            "tokens must come from the real issuer."
        )


def init(force: bool = False) -> int:
    """Generate the development keypair and say what to configure."""

    if PRIVATE_KEY.exists() and not force:
        print(f"  key already exists at {PRIVATE_KEY}")
        print("  re-run with --force to replace it (invalidates old tokens)")
        return 0

    KEY_DIR.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    PRIVATE_KEY.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    # Readable by this user only. It signs identities.
    PRIVATE_KEY.chmod(0o600)

    PUBLIC_KEY.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    print(f"  wrote {PRIVATE_KEY} (0600) and {PUBLIC_KEY}")
    print()
    print("  Add this to .env.development:")
    print()
    print("    JWT_ALGORITHM=RS256")
    print(f"    JWT_PUBLIC_KEY_PATH={PUBLIC_KEY}")
    print(f"    JWT_ISSUER={DEFAULT_ISSUER}")
    print(f"    JWT_AUDIENCE={DEFAULT_AUDIENCE}")
    print("    AUTH_DEV_MODE=false")
    print()
    print("  Then: python scripts/dev_token.py mint")

    return 0


def mint(subject: str, expires: int, issuer: str, audience: str) -> str:
    if not PRIVATE_KEY.exists():
        raise SystemExit(
            f"No development key at {PRIVATE_KEY}.\n"
            f"Run:  python scripts/dev_token.py init"
        )

    claims = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": int(time.time()),
        "exp": int(time.time()) + expires,
    }

    return jwt.encode(claims, PRIVATE_KEY.read_text(), algorithm="RS256")


def check() -> int:
    """
    Verify a freshly minted token through the API's own TokenVerifier.

    [claude] Worth its own command. The usual failure here is not a broken
    token but a mismatch — the issuer or audience in `.env` differing from
    the one used to sign — and the API reports every such failure as the
    same "Missing or invalid credentials", deliberately. This says which.
    """

    from app.auth.jwt import AuthError, TokenVerifier

    print(f"  APP_ENV            {APP_ENV}")
    print(f"  JWT_ALGORITHM      {settings.jwt_algorithm}")
    print(f"  JWT_PUBLIC_KEY_PATH {settings.jwt_public_key_path or '(unset)'}")
    print(f"  JWT_ISSUER         {settings.jwt_issuer or '(unset — not checked)'}")
    print(f"  JWT_AUDIENCE       {settings.jwt_audience or '(unset — not checked)'}")
    print(f"  AUTH_DEV_MODE      {settings.auth_dev_mode}")
    print()

    token = mint(
        DEFAULT_SUBJECT,
        300,
        settings.jwt_issuer or DEFAULT_ISSUER,
        settings.jwt_audience or DEFAULT_AUDIENCE,
    )

    try:
        principal = TokenVerifier(settings).verify(token)
    except AuthError as exc:
        print(f"  REJECTED: {exc.reason}")
        print()
        print("  The signing key or the issuer/audience in .env do not match.")
        return 1
    except RuntimeError as exc:
        print(f"  MISCONFIGURED: {exc}")
        return 1

    print(f"  accepted -> subject={principal.subject}")
    print(f"              workspace={principal.workspace_id}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="generate the development keypair")
    init_parser.add_argument("--force", action="store_true")

    for name, help_text in (
        ("mint", "print a bearer token"),
        ("header", "print a full Authorization header"),
    ):
        one = sub.add_parser(name, help=help_text)
        one.add_argument("--subject", default=DEFAULT_SUBJECT)
        one.add_argument(
            "--expires", type=int, default=86_400, help="seconds (default 24h)"
        )
        one.add_argument("--issuer", default=None)
        one.add_argument("--audience", default=None)

    sub.add_parser("check", help="verify a token through the API's verifier")

    args = parser.parse_args()

    refuse_in_production()

    if args.command == "init":
        return init(force=args.force)

    if args.command == "check":
        return check()

    token = mint(
        subject=args.subject,
        expires=args.expires,
        # Default to what the API is configured to expect, so a token minted
        # here is accepted there without anyone reconciling two files by eye.
        issuer=args.issuer or settings.jwt_issuer or DEFAULT_ISSUER,
        audience=args.audience or settings.jwt_audience or DEFAULT_AUDIENCE,
    )

    print(f"Authorization: Bearer {token}" if args.command == "header" else token)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
