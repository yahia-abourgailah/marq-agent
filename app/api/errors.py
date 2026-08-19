"""
[claude] Typed HTTP errors, and the rule that internals never cross the wire.

The same principle as design decision 6 in docs/HANDOFF.md — where only the
exception *type* crosses the tool boundary, because driver errors were leaking
DSN fragments into the model's context. At the HTTP edge the audience is a
browser rather than a model, so the rule is stricter: the client gets a stable
code and a sentence written for a person, and the detail goes to the log.

A traceback rendered into a JSON response is how a connection string, a table
name or a file path ends up in a front-end console.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("marq.api")


class ApiError(Exception):
    """
    An error with a client-safe message.

    Raised where the message is genuinely meant for the caller — "no such
    file", "that is too large". Anything unexpected is not one of these and
    becomes a 500 with a fixed string.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)

        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def _body(code: str, message: str, request_id: str | None) -> dict[str, Any]:
    """The one response shape every error uses."""

    return {
        "error": {
            "code": code,
            "message": message,
            # [claude] Echoed so a user reporting "it failed" gives us the
            # one string that finds the traceback in the log. It is the
            # only reason a 500 can afford to say nothing else.
            "request_id": request_id,
        }
    }


def request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers. Called by create_app()."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        logger.warning(
            "api_error",
            extra={
                "request_id": request_id_of(request),
                "code": exc.code,
                "path": request.url.path,
            },
        )

        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, exc.message, request_id_of(request)),
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(
                "http_error",
                str(exc.detail),
                request_id_of(request),
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # [claude] Pydantic's own errors are safe and genuinely useful to a
        # front-end developer — they name the field and what was wrong with
        # it — so these are passed through rather than flattened.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request body is not valid.",
                    "request_id": request_id_of(request),
                    "details": exc.errors(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # [claude] The important one. Anything not anticipated is logged in
        # full and reported as a fixed sentence — no exception text, no
        # type name, no path. An unhandled psycopg error here would
        # otherwise put a DSN fragment in a browser.
        logger.exception(
            "unhandled_error",
            extra={
                "request_id": request_id_of(request),
                "path": request.url.path,
            },
        )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_body(
                "internal_error",
                "Something went wrong handling that request.",
                request_id_of(request),
            ),
        )


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


__all__ = [
    "ApiError",
    "install_error_handlers",
    "new_request_id",
    "request_id_of",
]
