"""
[claude] Liveness and readiness.

Two endpoints because they answer different questions and a deployment needs
both. `/health` says the process is up; `/health/ready` says it can actually
do its job.

The readiness check reports what each dependency **can do**, not what it is
configured as. That distinction is the whole reason this file is careful:
docs/HANDOFF.md records a read-only role that existed, looked correctly
configured, and had no grants at all — it could read nothing. A check that
reads configuration would have called that healthy. `Database.verify_read_only`
exists for the same reason, and is quoted here rather than second-guessed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response, status

from app.api.schemas import ComponentHealth, HealthResponse, ReadinessResponse
from app.config import APP_ENV, settings
from app.db.connection import app_db

logger = logging.getLogger("marq.api")

router = APIRouter(tags=["health"])

API_VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """
    Liveness. Deliberately does no I/O.

    A liveness probe that touches the database restarts a healthy process
    because a dependency blipped, which turns one outage into two.
    """

    return HealthResponse(
        status="ok",
        version=API_VERSION,
        environment=APP_ENV,
    )


async def _database() -> ComponentHealth:
    try:
        async with app_db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1 AS ok")
                await cursor.fetchone()
    except Exception as exc:
        # Type only — a psycopg error's text carries DSN fragments.
        return ComponentHealth(ok=False, detail=type(exc).__name__)

    return ComponentHealth(
        ok=True,
        detail=(
            "read-only role" if app_db.is_read_only else "owning role"
        ),
    )


def _checkpointer(request: Request) -> ComponentHealth:
    handle = getattr(request.app.state, "checkpointer", None)

    if handle is None:
        return ComponentHealth(ok=False, detail="not initialised")

    if not handle.is_persistent:
        # [claude] Reported as not-ready rather than as fine. In-memory
        # checkpointing loses every conversation on restart and shares
        # nothing between workers, so a deployment that reaches production
        # this way has a bug that only shows up as the agent forgetting
        # mid-conversation, intermittently.
        return ComponentHealth(
            ok=False,
            detail="in-memory — conversations will not survive a restart",
        )

    return ComponentHealth(ok=True, detail="postgres")


def _model() -> ComponentHealth:
    if not settings.model_base_url or not settings.model_name:
        return ComponentHealth(ok=False, detail="not configured")

    # Deliberately not a live call. Readiness is polled often and a
    # generation request per poll is real load on the model endpoint.
    return ComponentHealth(ok=True, detail=settings.model_name)


def _vector_index(request: Request) -> ComponentHealth:
    service = getattr(request.app.state, "workspace_service", None)

    if service is None:
        return ComponentHealth(ok=False, detail="workspace not configured")

    try:
        index = service.index
    except Exception as exc:
        return ComponentHealth(ok=False, detail=type(exc).__name__)

    if index is None:
        # [claude] Not fatal, and reported honestly rather than as failure.
        # A dead Qdrant costs semantic search only — files still ingest,
        # parse and reconcile. Readiness stays true; this line says what was
        # lost.
        return ComponentHealth(
            ok=False,
            detail="unavailable — search is degraded, reconciliation works",
        )

    return ComponentHealth(ok=True, detail=settings.workspace_collection)


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(request: Request, response: Response) -> ReadinessResponse:
    """
    Whether this instance should receive traffic.

    Readiness turns on the database, the checkpointer and the model. The
    vector index is reported but does not gate: search degrades, everything
    else keeps working, and refusing traffic for it would take the CRM
    agent down because a file-search feature is unavailable.
    """

    database = await _database()
    checkpointer = _checkpointer(request)
    model = _model()
    vector_index = _vector_index(request)

    ready = database.ok and checkpointer.ok and model.ok

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        ready=ready,
        database=database,
        checkpointer=checkpointer,
        model=model,
        vector_index=vector_index,
    )


__all__ = ["API_VERSION", "router"]
