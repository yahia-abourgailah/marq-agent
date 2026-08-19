"""
[claude] Upload, list and delete files — the endpoint docs/HANDOFF.md records
as missing ("Not wired up: there is no upload endpoint. `app/api/` is still
empty").

It is a thin wrapper over `WorkspaceService`, on purpose. That service is the
same path the agent's tools use, so these routes exercise the code that has
already been hardened rather than a parallel one that could drift — which is
what the handoff means by "the endpoints wrap the service without reworking
it".

The workspace is never named by the caller. It is derived from the verified
token by `Principal.workspace_id`, which is what closes the gap the handoff
flags: "whoever sets workspace_id — the API layer, when it exists — must
derive it from an authenticated session and never from user input."
"""

from __future__ import annotations

import logging

from anyio import to_thread
from fastapi import APIRouter, File, Request, UploadFile, status

from app.api.deps import CurrentPrincipal, Workspace
from app.api.errors import ApiError
from app.api.schemas import (
    DeleteResponse,
    FileList,
    UploadedFile,
    UploadedSheet,
    UploadResponse,
)
from app.config import settings
from app.workspace.store import WorkspaceError

logger = logging.getLogger("marq.api")

router = APIRouter(prefix="/workspace", tags=["workspace"])

# Read in chunks so the size limit can be enforced while reading rather than
# after. See _read_within_limit.
_CHUNK = 1024 * 1024


def _as_file(entry) -> UploadedFile:
    return UploadedFile(
        file_id=entry.file_id,
        filename=entry.filename,
        kind=str(entry.kind),
        byte_size=entry.byte_size,
        sheets=[
            UploadedSheet(
                name=sheet.name,
                row_count=sheet.row_count,
                columns=[column.name for column in sheet.columns],
            )
            for sheet in entry.sheets
        ],
        page_count=entry.page_count,
    )


async def _read_within_limit(upload: UploadFile, limit: int) -> bytes:
    """
    Read the upload, refusing once it exceeds the limit.

    [claude] Read incrementally rather than with `await upload.read()`.

    The whole-file form only discovers the size after the bytes are already
    in memory, so a caller sending a 2 GB body has already spent 2 GB before
    anything checks. Content-Length is not a substitute — it is supplied by
    the client and a chunked request has none.

    `ingest_file` also enforces a limit, and that is not redundant: it is the
    backstop for every non-HTTP caller (the CLI, the tests, the evals). This
    one exists so the HTTP path never buffers what it is going to reject.
    """

    chunks: list[bytes] = []
    total = 0

    while chunk := await upload.read(_CHUNK):
        total += len(chunk)

        if total > limit:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "file_too_large",
                f"That file is larger than the {limit // 1_048_576} MB limit.",
            )

        chunks.append(chunk)

    return b"".join(chunks)


@router.post(
    "/files",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    request: Request,
    principal: CurrentPrincipal,
    service: Workspace,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI's form-field marker
) -> UploadResponse:
    """
    Upload one file into the caller's workspace.

    Re-uploading a filename supersedes the previous copy, vectors and all —
    that behaviour lives in the service and is relied on here rather than
    reimplemented.
    """

    if not file.filename:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "missing_filename",
            "The upload has no filename, so its type cannot be determined.",
        )

    data = await _read_within_limit(file, settings.max_upload_bytes)

    try:
        # [claude] Off the event loop. Ingestion parses the whole file and
        # then embeds it — both synchronous and both CPU-bound, taking
        # seconds on a real export. Run inline it would block every other
        # request on this worker, so a single upload stalls everyone's chat.
        result = await to_thread.run_sync(
            lambda: service.ingest(
                principal.workspace_id, file.filename, data
            )
        )
    except WorkspaceError as exc:
        # Client-safe by construction: these messages are written for a
        # person and already reach the model through the tools.
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "upload_rejected",
            str(exc),
        ) from exc

    logger.info(
        "file_uploaded",
        extra={
            "subject": principal.subject,
            "file_id": result.file.file_id,
            "bytes": len(data),
        },
    )

    return UploadResponse(
        file=_as_file(result.file),
        chunks_indexed=result.chunks_indexed,
        warnings=list(result.warnings),
    )


@router.get("/files", response_model=FileList)
async def list_files(
    principal: CurrentPrincipal,
    service: Workspace,
) -> FileList:
    """Everything uploaded to the caller's workspace."""

    entries = await to_thread.run_sync(
        lambda: service.list_files(principal.workspace_id)
    )

    return FileList(files=[_as_file(entry) for entry in entries])


@router.delete("/files/{file_id}", response_model=DeleteResponse)
async def delete_file(
    file_id: str,
    principal: CurrentPrincipal,
    service: Workspace,
) -> DeleteResponse:
    """
    Remove one file and its vectors.

    The service scopes every call by workspace, so a file id belonging to
    someone else is simply not found here.
    """

    try:
        await to_thread.run_sync(
            lambda: service.delete(principal.workspace_id, file_id)
        )
    except WorkspaceError as exc:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            str(exc),
        ) from exc

    logger.info(
        "file_deleted",
        extra={"subject": principal.subject, "file_id": file_id},
    )

    return DeleteResponse(deleted=True)


__all__ = ["router"]
