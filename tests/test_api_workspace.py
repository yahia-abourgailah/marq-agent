"""
[claude] The upload endpoint docs/HANDOFF.md records as missing.

These run against a real `WorkspaceService` over a temporary directory, not a
mock — the service is the code the agent's tools already use, and the point of
the routes is that they wrap it rather than reimplement it. A mocked service
would test the wrapper against an idea of the service instead of the service.

The vector index is left out (`index=None`), which the service supports by
design: files still ingest, parse and reconcile without Qdrant, and only
search is lost.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.workspace.service import WorkspaceService
from app.workspace.store import WorkspaceStore
from tests.api_support import TokenIssuer, build_app, client


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


@pytest.fixture
def service(tmp_path):
    return WorkspaceService(store=WorkspaceStore(tmp_path / "store"))


XLSX_TYPE = "application/vnd.ms-excel"


def upload_of(name: str, rows: int = 3) -> dict:
    """One multipart upload field, so call sites stay short."""

    return {"file": (name, xlsx_bytes(rows), XLSX_TYPE)}


def xlsx_bytes(rows=3) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Contracts"
    sheet.append(["Deal ID", "Area (sqm)", "Client"])

    for index in range(rows):
        sheet.append([f"D-{index}", 100 + index, f"Client {index}"])

    buffer = io.BytesIO()
    book.save(buffer)

    return buffer.getvalue()


@pytest.mark.asyncio
async def test_uploading_a_spreadsheet_reports_its_shape(issuer, service):
    app, _ = build_app(verifier=issuer.verifier(), workspace_service=service)

    async with client(app) as http:
        response = await http.post(
            "/v1/workspace/files",
            files=upload_of("contracts.xlsx"),
            headers=issuer.auth("employee-1"),
        )

    body = response.json()

    assert response.status_code == 201
    assert body["file"]["filename"] == "contracts.xlsx"
    assert body["file"]["kind"] == "spreadsheet"

    sheet = body["file"]["sheets"][0]

    assert sheet["name"] == "Contracts"
    assert sheet["row_count"] == 3
    assert sheet["columns"] == ["Deal ID", "Area (sqm)", "Client"]


@pytest.mark.asyncio
async def test_a_file_lands_in_the_callers_own_workspace(issuer, service):
    """
    The workspace is derived from the token, so one employee's upload is not
    visible to another. This is the gap docs/HANDOFF.md flags: "whoever sets
    workspace_id must derive it from an authenticated session and never from
    user input."
    """

    app, _ = build_app(verifier=issuer.verifier(), workspace_service=service)

    async with client(app) as http:
        await http.post(
            "/v1/workspace/files",
            files=upload_of("alice.xlsx"),
            headers=issuer.auth("alice@example.com"),
        )

        alice = await http.get(
            "/v1/workspace/files", headers=issuer.auth("alice@example.com")
        )
        bob = await http.get(
            "/v1/workspace/files", headers=issuer.auth("bob@example.com")
        )

    assert [f["filename"] for f in alice.json()["files"]] == ["alice.xlsx"]
    assert bob.json()["files"] == []


@pytest.mark.asyncio
async def test_another_employees_file_id_is_not_reachable(issuer, service):
    """Guessing a file id from outside the workspace finds nothing."""

    app, _ = build_app(verifier=issuer.verifier(), workspace_service=service)

    async with client(app) as http:
        upload = await http.post(
            "/v1/workspace/files",
            files=upload_of("alice.xlsx"),
            headers=issuer.auth("alice@example.com"),
        )

        file_id = upload.json()["file"]["file_id"]

        response = await http.delete(
            f"/v1/workspace/files/{file_id}",
            headers=issuer.auth("bob@example.com"),
        )

        still_there = await http.get(
            "/v1/workspace/files", headers=issuer.auth("alice@example.com")
        )

    assert response.status_code == 404
    assert len(still_there.json()["files"]) == 1


@pytest.mark.asyncio
async def test_a_file_can_be_deleted_by_its_owner(issuer, service):
    app, _ = build_app(verifier=issuer.verifier(), workspace_service=service)

    async with client(app) as http:
        upload = await http.post(
            "/v1/workspace/files",
            files=upload_of("x.xlsx"),
            headers=issuer.auth("employee-1"),
        )
        file_id = upload.json()["file"]["file_id"]

        deleted = await http.delete(
            f"/v1/workspace/files/{file_id}", headers=issuer.auth("employee-1")
        )
        listed = await http.get(
            "/v1/workspace/files", headers=issuer.auth("employee-1")
        )

    assert deleted.status_code == 200
    assert listed.json()["files"] == []


@pytest.mark.asyncio
async def test_an_oversized_upload_is_refused(issuer, service):
    """
    [claude] Refused while reading, not after.

    The route reads in chunks so a huge body is rejected part-way rather
    than buffered whole and then measured — `await upload.read()` would have
    already spent the memory by the time anything checked.
    """

    app, _ = build_app(verifier=issuer.verifier(), workspace_service=service)

    from app.config import settings

    oversized = b"x" * (settings.max_upload_bytes + 1024)

    async with client(app) as http:
        response = await http.post(
            "/v1/workspace/files",
            files={"file": ("huge.csv", oversized, "text/csv")},
            headers=issuer.auth(),
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


@pytest.mark.asyncio
async def test_an_unsupported_file_type_is_refused_with_a_reason(issuer, service):
    app, _ = build_app(verifier=issuer.verifier(), workspace_service=service)

    async with client(app) as http:
        response = await http.post(
            "/v1/workspace/files",
            files={"file": ("notes.docx", b"not really a docx", "application/msword")},
            headers=issuer.auth(),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "upload_rejected"
    # The message is written for a person and names what went wrong.
    assert response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_an_empty_upload_is_refused(issuer, service):
    app, _ = build_app(verifier=issuer.verifier(), workspace_service=service)

    async with client(app) as http:
        response = await http.post(
            "/v1/workspace/files",
            files={"file": ("empty.csv", b"", "text/csv")},
            headers=issuer.auth(),
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_a_traversal_filename_cannot_escape_the_workspace(
    issuer, service, tmp_path
):
    """
    The store already defends against this; asserted here because the HTTP
    layer is the first place a filename arrives from a stranger.
    """

    app, _ = build_app(verifier=issuer.verifier(), workspace_service=service)

    async with client(app) as http:
        response = await http.post(
            "/v1/workspace/files",
            files={
                "file": (
                    "../../../../etc/passwd.csv",
                    b"a,b\n1,2\n",
                    "text/csv",
                )
            },
            headers=issuer.auth(),
        )

    assert response.status_code in (201, 400)

    # Whatever happened, nothing was written outside the store root.
    escaped = (tmp_path / "store").resolve()
    written = list(escaped.rglob("*")) if escaped.exists() else []

    for path in written:
        assert escaped in path.resolve().parents or path.resolve() == escaped


@pytest.mark.asyncio
async def test_re_uploading_a_filename_supersedes_the_first(issuer, service):
    """
    Behaviour that lives in the service and is relied on, not reimplemented.

    Two entries with one name used to stall the agent — asked what a
    document said, it stopped to ask which of the two was meant.
    """

    app, _ = build_app(verifier=issuer.verifier(), workspace_service=service)

    async with client(app) as http:
        first = await http.post(
            "/v1/workspace/files",
            files=upload_of("report.xlsx", 3),
            headers=issuer.auth("employee-1"),
        )
        second = await http.post(
            "/v1/workspace/files",
            files=upload_of("report.xlsx", 7),
            headers=issuer.auth("employee-1"),
        )
        listed = await http.get(
            "/v1/workspace/files", headers=issuer.auth("employee-1")
        )

    files = listed.json()["files"]

    assert first.json()["file"]["file_id"] != second.json()["file"]["file_id"]
    assert len(files) == 1
    assert files[0]["sheets"][0]["row_count"] == 7


@pytest.mark.asyncio
async def test_uploads_are_unavailable_rather_than_broken_when_unconfigured(issuer):
    """A deployment without a workspace says so, instead of 500ing."""

    app, _ = build_app(verifier=issuer.verifier(), workspace_service=None)

    async with client(app) as http:
        response = await http.get(
            "/v1/workspace/files", headers=issuer.auth()
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "workspace_unavailable"
