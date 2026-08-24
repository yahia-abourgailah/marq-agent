"""
[claude] The workspace tools, exercised the way the agent calls them.

These run the whole stack except the model: real files on disk, real parsing,
a real in-process Qdrant, and the tool functions themselves. Only the encoder
is faked, because retrieval quality is not what these assert.

The tools are invoked through `.coroutine` with a hand-built runtime. That is
deliberate rather than convenient: it is the same path the agent loop takes,
and it lets a test prove that a workspace id the model supplied would go
nowhere — there is no argument for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools.workspace import UNTRUSTED_NOTE, WorkspaceContext, build_workspace_tools
from app.workspace.index import build_index
from app.workspace.service import WorkspaceService
from app.workspace.store import WorkspaceStore
from tests.workspace_support import FakeEmbedder, build_csv, build_pdf


class FakeRuntime:
    """Stands in for LangGraph's ToolRuntime, carrying only the context."""

    def __init__(self, workspace_id: str | None) -> None:
        self.context = WorkspaceContext(workspace_id=workspace_id)


@pytest.fixture
def service(tmp_path):
    embedder = FakeEmbedder()

    return WorkspaceService(
        store=WorkspaceStore(tmp_path / "store"),
        index=build_index(collection="tools", dimension=embedder.dimension),
        embedder=embedder,
    )


@pytest.fixture
def tools(service):
    return {tool.name: tool for tool in build_workspace_tools(service)}


@pytest.fixture
def deals_csv(tmp_path) -> Path:
    return build_csv(
        tmp_path / "deals.csv",
        header=["deal_id", "amount", "status"],
        rows=[
            ["D-1", "1000", "contracted"],
            ["D-2", "2500", "contracted"],
            ["D-3", "400", "eoi"],
        ],
    )


async def call(tool, workspace_id: str | None = "ws1", **kwargs):
    return await tool.coroutine(runtime=FakeRuntime(workspace_id), **kwargs)


# ============================================================
# The workspace id is not something the model can set
# ============================================================


def test_no_tool_exposes_workspace_id_as_an_argument(tools):
    """
    [claude] The load-bearing assertion of the whole isolation design. If
    `workspace_id` ever appears in a tool's schema, a model can name someone
    else's workspace — and a prompt-injected instruction inside an uploaded
    file could tell it which one.
    """

    for tool in tools.values():
        assert "workspace_id" not in tool.args


@pytest.mark.asyncio
async def test_tools_report_plainly_when_no_workspace_is_attached(tools):
    result = await call(tools["workspace_files"], workspace_id=None)

    assert result["success"] is False
    assert "no uploaded files" in result["error"]


# ============================================================
# Manifest
# ============================================================


@pytest.mark.asyncio
async def test_workspace_files_lists_shape_without_values(
    tools, service, deals_csv
):
    service.ingest_path("ws1", deals_csv)

    result = await call(tools["workspace_files"])

    assert result["success"] is True
    assert result["file_count"] == 1

    described = result["files"][0]

    assert described["filename"] == "deals.csv"
    assert described["kind"] == "spreadsheet"
    assert described["sheets"][0]["rows"] == 3
    assert [c["name"] for c in described["sheets"][0]["columns"]] == [
        "deal_id",
        "amount",
        "status",
    ]

    # No cell values anywhere in the manifest.
    assert "D-1" not in str(result)


@pytest.mark.asyncio
async def test_an_empty_workspace_reports_zero_files(tools):
    result = await call(tools["workspace_files"])

    assert result["success"] is True
    assert result["file_count"] == 0


@pytest.mark.asyncio
async def test_files_are_not_visible_from_another_workspace(
    tools, service, deals_csv
):
    service.ingest_path("ws1", deals_csv)

    result = await call(tools["workspace_files"], workspace_id="ws2")

    assert result["file_count"] == 0


# ============================================================
# Exact reads
# ============================================================


@pytest.mark.asyncio
async def test_read_rows_returns_complete_filtered_results(
    tools, service, deals_csv
):
    file_id = service.ingest_path("ws1", deals_csv).file.file_id

    result = await call(
        tools["workspace_read_rows"],
        file_id=file_id,
        filters=[{"column": "status", "op": "eq", "value": "contracted"}],
    )

    assert result["success"] is True
    assert result["matched_rows"] == 2
    assert result["truncated"] is False
    assert [row["deal_id"] for row in result["rows"]] == ["D-1", "D-2"]
    assert result["note"] == UNTRUSTED_NOTE


@pytest.mark.asyncio
async def test_read_rows_flags_truncation_so_a_sample_is_not_read_as_a_total(
    tools, service, deals_csv
):
    file_id = service.ingest_path("ws1", deals_csv).file.file_id

    result = await call(
        tools["workspace_read_rows"], file_id=file_id, limit=1
    )

    assert result["matched_rows"] == 3
    assert result["returned_rows"] == 1
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_a_bad_column_is_retryable_and_names_the_real_columns(
    tools, service, deals_csv
):
    file_id = service.ingest_path("ws1", deals_csv).file.file_id

    result = await call(
        tools["workspace_read_rows"],
        file_id=file_id,
        filters=[{"column": "nope", "op": "eq", "value": 1}],
    )

    assert result["success"] is False
    assert result["retryable"] is True
    assert "deal_id" in result["error"]


@pytest.mark.asyncio
async def test_reading_another_workspaces_file_by_id_fails(
    tools, service, deals_csv
):
    """Holding the file id must not be enough."""

    file_id = service.ingest_path("ws1", deals_csv).file.file_id

    result = await call(
        tools["workspace_read_rows"], workspace_id="ws2", file_id=file_id
    )

    assert result["success"] is False


@pytest.mark.asyncio
async def test_reading_a_pdf_as_a_spreadsheet_says_to_search_it_instead(
    tools, service, tmp_path
):
    pdf = build_pdf(tmp_path / "c.pdf", ["Some contract text."])
    file_id = service.ingest_path("ws1", pdf).file.file_id

    result = await call(tools["workspace_read_rows"], file_id=file_id)

    assert result["success"] is False
    assert "workspace_search" in result["error"]


# ============================================================
# Aggregates
# ============================================================


@pytest.mark.asyncio
async def test_aggregate_computes_over_every_row(tools, service, deals_csv):
    file_id = service.ingest_path("ws1", deals_csv).file.file_id

    result = await call(
        tools["workspace_aggregate"],
        file_id=file_id,
        operation="sum",
        column="amount",
    )

    assert result["value"] == 3900  # 1000 + 2500 + 400


@pytest.mark.asyncio
async def test_aggregate_groups(tools, service, deals_csv):
    file_id = service.ingest_path("ws1", deals_csv).file.file_id

    result = await call(
        tools["workspace_aggregate"],
        file_id=file_id,
        operation="sum",
        column="amount",
        group_by="status",
    )

    assert result["groups"]["contracted"]["value"] == 3500
    assert result["groups"]["eoi"]["value"] == 400


# ============================================================
# Search
# ============================================================


@pytest.mark.asyncio
async def test_search_returns_citations_and_marks_content_untrusted(
    tools, service, tmp_path
):
    pdf = build_pdf(
        tmp_path / "contract.pdf",
        ["Payment terms are net thirty days.", "Penalties apply after sixty."],
    )
    service.ingest_path("ws1", pdf)

    result = await call(
        tools["workspace_search"], question="payment terms net thirty"
    )

    assert result["success"] is True
    assert result["note"] == UNTRUSTED_NOTE

    # Never claims completeness — the agent must not total these.
    assert result["complete"] is False

    sources = [hit["source"] for hit in result["results"]]
    assert any("contract.pdf, page" in source for source in sources)


@pytest.mark.asyncio
async def test_search_does_not_cross_workspaces(tools, service, tmp_path):
    pdf = build_pdf(tmp_path / "secret.pdf", ["Confidential alpha figures."])
    service.ingest_path("ws1", pdf)

    result = await call(
        tools["workspace_search"],
        workspace_id="ws2",
        question="confidential alpha figures",
    )

    assert result["success"] is True
    assert result["results"] == []


@pytest.mark.asyncio
async def test_searching_another_workspaces_file_id_fails(
    tools, service, tmp_path
):
    pdf = build_pdf(tmp_path / "a.pdf", ["Some text."])
    file_id = service.ingest_path("ws1", pdf).file.file_id

    result = await call(
        tools["workspace_search"],
        workspace_id="ws2",
        question="some text",
        file_id=file_id,
    )

    assert result["success"] is False


@pytest.mark.asyncio
async def test_search_degrades_rather_than_raising_without_an_index(tmp_path):
    """Qdrant being unavailable costs search, not the whole workspace."""

    service = WorkspaceService(store=WorkspaceStore(tmp_path))
    tools = {tool.name: tool for tool in build_workspace_tools(service)}

    result = await call(tools["workspace_search"], question="anything")

    assert result["success"] is False
    assert result["reason"] == "not_available"


# ============================================================
# Comparison
# ============================================================


@pytest.mark.asyncio
async def test_compare_lines_up_file_rows_against_crm_rows(tools):
    result = await tools["compare_with_crm"].coroutine(
        uploaded_rows=[
            {"deal_id": "D-1", "amount": 1000},
            {"deal_id": "D-2", "amount": 2500},
            {"deal_id": "D-3", "amount": 400},
        ],
        crm_rows=[
            {"id": "D-1", "amount": 1000},
            {"id": "D-2", "amount": 9999},
            {"id": "D-9", "amount": 50},
        ],
        key="deal_id",
        crm_key="id",
        value_columns=["amount"],
    )

    assert result["totals"]["matched"] == 1
    assert result["totals"]["mismatched"] == 1
    assert result["totals"]["only_in_uploaded"] == 1
    assert result["totals"]["only_in_crm"] == 1


@pytest.mark.asyncio
async def test_compare_rejects_oversized_payloads_retryably(tools):
    rows = [{"id": i} for i in range(6_000)]

    result = await tools["compare_with_crm"].coroutine(
        uploaded_rows=rows, crm_rows=[], key="id"
    )

    assert result["success"] is False
    assert result["retryable"] is True


@pytest.mark.asyncio
async def test_compare_with_a_missing_key_is_retryable(tools):
    result = await tools["compare_with_crm"].coroutine(
        uploaded_rows=[{"amount": 1}], crm_rows=[{"id": 1}], key="deal_id"
    )

    assert result["success"] is False
    assert result["retryable"] is True


# ============================================================
# End to end, the way the agent works
# ============================================================


@pytest.mark.asyncio
async def test_the_full_reconciliation_path(tools, service, deals_csv):
    """
    Manifest, then exact rows, then compare — the sequence the system prompt
    lays out, with a CRM side standing in for sql_query.
    """

    service.ingest_path("ws1", deals_csv)

    manifest = await call(tools["workspace_files"])
    file_id = manifest["files"][0]["file_id"]

    rows = await call(
        tools["workspace_read_rows"],
        file_id=file_id,
        columns=["deal_id", "amount"],
    )

    assert rows["truncated"] is False

    crm_rows = [
        {"id": "D-1", "amount": 1000},
        {"id": "D-2", "amount": 2500},
        {"id": "D-3", "amount": 999},
    ]

    comparison = await tools["compare_with_crm"].coroutine(
        uploaded_rows=rows["rows"],
        crm_rows=crm_rows,
        key="deal_id",
        crm_key="id",
        value_columns=["amount"],
    )

    assert comparison["totals"]["matched"] == 2
    assert comparison["totals"]["mismatched"] == 1

    difference = comparison["mismatched"][0]["differences"][0]

    # The file says 400, the CRM says 999.
    assert difference["uploaded"] == "400"
    assert difference["crm"] == 999


# ============================================================
# [claude] Re-uploading the same filename — found by pressure testing.
# ============================================================


@pytest.mark.asyncio
async def test_reuploading_a_filename_replaces_the_earlier_copy(
    service, tmp_path, tools
):
    """
    Two entries with one name stalled the agent: asked what a document said,
    it stopped to ask which of the two identically-named files was meant.
    Re-uploading a corrected sheet is the most common workflow there is, so
    the second upload supersedes the first.
    """

    first = build_csv(
        tmp_path / "deals.csv", header=["id", "amount"], rows=[["1", "100"]]
    )
    service.ingest_path("ws1", first)

    second = build_csv(
        tmp_path / "deals.csv",
        header=["id", "amount"],
        rows=[["1", "999"], ["2", "5"]],
    )
    result = service.ingest_path("ws1", second)

    files = service.list_files("ws1")

    assert len(files) == 1
    assert any("replaced an earlier upload" in w for w in result.warnings)

    # The surviving copy is the new one.
    rows = service.read_rows("ws1", files[0].file_id)
    assert rows["matched_rows"] == 2


@pytest.mark.asyncio
async def test_replacing_a_file_removes_its_stale_chunks(service, tmp_path):
    """
    A stale vector outliving its file would let search quote content that is
    no longer in the workspace.
    """

    path = build_csv(
        tmp_path / "notes.csv", header=["note"], rows=[["alpha unique text"]]
    )
    first_id = service.ingest_path("ws1", path).file.file_id

    replacement = build_csv(
        tmp_path / "notes.csv", header=["note"], rows=[["beta different text"]]
    )
    service.ingest_path("ws1", replacement)

    hits, _ = service.search("ws1", "alpha unique text", limit=10)

    assert all(hit.chunk.locator.file_id != first_id for hit in hits)


@pytest.mark.asyncio
async def test_different_filenames_coexist(service, tmp_path):
    """Replacement is by name, so unrelated files are untouched."""

    a = build_csv(tmp_path / "a.csv", header=["id"], rows=[["1"]])
    b = build_csv(tmp_path / "b.csv", header=["id"], rows=[["2"]])

    service.ingest_path("ws1", a)
    service.ingest_path("ws1", b)

    assert len(service.list_files("ws1")) == 2
