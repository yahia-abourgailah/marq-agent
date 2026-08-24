"""
[claude] Request-scoped dependencies.

Everything expensive — the graph, the checkpointer, the connection pools, the
workspace service — is built once in the lifespan and read from `app.state`
here. Building a graph per request would construct a model client, a SQL agent
and a guard for every question.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends, Header, Request, status

from app.api.errors import ApiError
from app.api.ratelimit import RateLimiter
from app.auth.jwt import AuthError, TokenVerifier
from app.auth.principal import Principal
from app.config import settings
from app.db.repositories.conversations import ConversationRepository

logger = logging.getLogger("marq.api")

# [claude] One message for every authentication failure, deliberately.
#
# Distinguishing "expired" from "bad signature" from "unknown subject" tells
# an attacker which half of a guess was right. The specific reason is logged;
# the caller gets one sentence.
_UNAUTHORIZED = "Missing or invalid credentials."


def get_verifier(request: Request) -> TokenVerifier:
    return request.app.state.verifier


def get_graph(request: Request):
    return request.app.state.graph


def get_conversations(request: Request) -> ConversationRepository:
    repository = request.app.state.conversations

    if repository is None:
        # Reachable only when the API runs on the in-memory checkpointer,
        # where there is no state database to index into.
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "conversations_unavailable",
            "Conversation history is not available in this deployment.",
        )

    return repository


def get_workspace_service(request: Request):
    service = request.app.state.workspace_service

    if service is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "workspace_unavailable",
            "File uploads are not available in this deployment.",
        )

    return service


async def get_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_debug_subject: Annotated[str | None, Header()] = None,
) -> Principal:
    """
    The authenticated caller, or a 401.

    This is the only place a `Principal` is minted. Every route that touches
    data depends on it, and every identifier that reaches the graph is read
    off it — so there is no path to CRM data that skipped authentication.
    """

    verifier: TokenVerifier = request.app.state.verifier

    # ----------------------------------------------------------
    # Local development without a token issuer
    # ----------------------------------------------------------
    #
    # [claude] Guarded three ways rather than one, because the failure mode
    # is silent and total: this branch accepts an identity from an unsigned
    # header, so anything reaching it can read any employee's data.
    #
    #   1. settings.auth_dev_mode must be on.
    #   2. TokenVerifier refuses to construct at all when it is on under
    #      APP_ENV=production, so the process does not start.
    #   3. The header is ignored entirely whenever a bearer token is present,
    #      so it cannot be used to override a real identity.
    if verifier.dev_mode and not authorization:
        if not x_debug_subject or not x_debug_subject.strip():
            raise ApiError(
                status.HTTP_401_UNAUTHORIZED,
                "unauthorized",
                "Development mode: send X-Debug-Subject or a bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        logger.warning(
            "dev_mode_identity_accepted",
            extra={"subject": x_debug_subject.strip()},
        )

        return Principal(subject=x_debug_subject.strip())

    if not authorization:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            _UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.partition(" ")

    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            _UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        principal = verifier.verify(token.strip())
    except AuthError as exc:
        # The reason goes to the log; the caller gets the fixed sentence.
        logger.info("auth_failed", extra={"reason": exc.reason})

        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            _UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    _enforce_rate_limit(principal)

    return principal


# [claude] One limiter per process, built lazily so `settings` is read at
# first use rather than at import — which is what lets a test override the
# limit without reimporting the module.
_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter

    if _limiter is None or _limiter.per_minute != settings.rate_limit_per_minute:
        _limiter = RateLimiter(settings.rate_limit_per_minute)

    return _limiter


def reset_rate_limiter() -> None:
    """
    [claude] Drop the counters. Used by the test suite between cases.

    A process-wide limiter is right in production and wrong for a suite
    that makes hundreds of requests as one subject — without this, the
    first thirty tests pass and the rest get 429s that have nothing to do
    with what they assert. Exposed as a function rather than having tests
    reach for the module global, so the reset survives the limiter being
    reimplemented.
    """

    global _limiter

    _limiter = None


def _enforce_rate_limit(principal: Principal) -> None:
    """
    [claude] Applied after the token is verified, on purpose.

    Keying on the subject means the key has to be trustworthy, and it is
    only trustworthy once the signature has been checked. Rate-limiting
    before authentication would mean limiting on something the caller
    controls, which is not a limit.

    The cost is that an unauthenticated flood still reaches the verifier.
    That is cheap — a signature check, no database, no model — and it is
    the right place to stop it if it ever needs stopping.
    """

    limiter = get_rate_limiter()
    allowed, retry_after = limiter.check(principal.subject)

    # Cheap, and only on the request that is already being rejected.
    if not allowed:
        limiter.prune()

        logger.warning(
            "rate_limited",
            extra={
                "subject": principal.subject,
                "limit_per_minute": settings.rate_limit_per_minute,
                "retry_after": retry_after,
            },
        )

        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            "Too many requests. Wait a moment and try again.",
            headers={"Retry-After": str(retry_after)},
        )


# [claude] Annotated aliases rather than `= Depends(...)` defaults. Same
# wiring, but it keeps the dependency out of the function signature's default
# values — which ruff's bugbear rules reject, and which reads better anyway
# because the parameter keeps its real type.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Conversations = Annotated[ConversationRepository, Depends(get_conversations)]
Graph = Annotated[Any, Depends(get_graph)]
Workspace = Annotated[Any, Depends(get_workspace_service)]


def max_upload_bytes() -> int:
    return settings.max_upload_bytes


__all__ = [
    "Conversations",
    "CurrentPrincipal",
    "Graph",
    "Workspace",
    "get_conversations",
    "get_graph",
    "get_principal",
    "get_verifier",
    "get_workspace_service",
    "max_upload_bytes",
]
