"""
[claude] Storage, identifiers and isolation.

The isolation tests here are the ones that matter. Everything else in the
workspace is a convenience; a store that can be talked into returning another
workspace's file is a data breach.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.workspace.models import (
    ColumnSpec,
    FileKind,
    PageContent,
    ParsedFile,
    SheetContent,
    SheetSummary,
    WorkspaceFile,
)
from app.workspace.store import (
    WorkspaceError,
    WorkspaceStore,
    classify_extension,
    new_file_id,
    validate_slug,
)


def make_entry(workspace_id: str, file_id: str = "wf_test") -> WorkspaceFile:
    return WorkspaceFile(
        file_id=file_id,
        workspace_id=workspace_id,
        filename="deals.csv",
        kind=FileKind.SPREADSHEET,
        byte_size=42,
        ingested_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        sheets=(
            SheetSummary(
                name="deals",
                row_count=2,
                columns=(ColumnSpec("id", "number", 2),),
            ),
        ),
    )


SHEET = ParsedFile(
    kind=FileKind.SPREADSHEET,
    sheets=(
        SheetContent(
            name="deals",
            columns=(ColumnSpec("id", "number", 2),),
            rows=({"id": 1}, {"id": 2}),
        ),
    ),
)


# ============================================================
# Identifiers
# ============================================================


@pytest.mark.parametrize(
    "value",
    [
        "../etc",
        "..",
        "a/b",
        "a\\b",
        "",
        "-leading",
        "with space",
        "x" * 65,
        "workspace\x00",
    ],
)
def test_validate_slug_rejects_anything_that_could_escape(value):
    with pytest.raises(WorkspaceError):
        validate_slug(value, "workspace id")


@pytest.mark.parametrize("value", ["ws1", "user-42", "a_b-C9", "x" * 64])
def test_validate_slug_accepts_ordinary_identifiers(value):
    assert validate_slug(value, "workspace id") == value


def test_new_file_id_is_a_valid_slug():
    for _ in range(20):
        validate_slug(new_file_id(), "file id")


def test_classify_extension_maps_to_a_lane():
    assert classify_extension("a.pdf") == (".pdf", FileKind.DOCUMENT)
    assert classify_extension("a.XLSX") == (".xlsx", FileKind.SPREADSHEET)
    assert classify_extension("a.csv") == (".csv", FileKind.SPREADSHEET)


def test_classify_extension_rejects_unsupported_types():
    with pytest.raises(WorkspaceError, match="Unsupported file type"):
        classify_extension("payload.exe")

    with pytest.raises(WorkspaceError):
        classify_extension("no_extension")


# ============================================================
# Registry
# ============================================================


def test_registry_round_trips_enum_and_timestamp(tmp_path):
    """
    [claude] The failure this guards against is silent: dataclasses.asdict
    would store the enum and the datetime as-is, and the reload would hand
    back strings where the code expects objects — surfacing much later as an
    attribute error inside a tool.
    """

    store = WorkspaceStore(tmp_path)
    entry = make_entry("ws1")

    store.save_file(entry, SHEET)

    loaded = store.get_file("ws1", "wf_test")

    assert loaded.kind is FileKind.SPREADSHEET
    assert loaded.ingested_at == entry.ingested_at
    assert loaded.ingested_at.tzinfo is not None
    assert loaded.sheets[0].columns[0].dtype == "number"
    assert loaded.sheets[0].row_count == 2


def test_parsed_content_round_trips_exactly(tmp_path):
    store = WorkspaceStore(tmp_path)
    store.save_file(make_entry("ws1"), SHEET)

    parsed = store.load_parsed("ws1", "wf_test")

    assert parsed.kind is FileKind.SPREADSHEET
    assert parsed.sheets[0].rows == ({"id": 1}, {"id": 2})


def test_document_parsed_content_round_trips(tmp_path):
    store = WorkspaceStore(tmp_path)

    entry = WorkspaceFile(
        file_id="wf_doc",
        workspace_id="ws1",
        filename="contract.pdf",
        kind=FileKind.DOCUMENT,
        byte_size=10,
        ingested_at=datetime.now(UTC),
        page_count=2,
    )

    parsed = ParsedFile(
        kind=FileKind.DOCUMENT,
        pages=(PageContent(1, "first"), PageContent(2, "second")),
    )

    store.save_file(entry, parsed)

    reloaded = store.load_parsed("ws1", "wf_doc")

    assert [page.text for page in reloaded.pages] == ["first", "second"]


def test_saving_the_same_file_id_twice_does_not_duplicate_it(tmp_path):
    store = WorkspaceStore(tmp_path)

    store.save_file(make_entry("ws1"), SHEET)
    store.save_file(make_entry("ws1"), SHEET)

    assert len(store.list_files("ws1")) == 1


def test_delete_removes_the_entry_and_its_parsed_content(tmp_path):
    store = WorkspaceStore(tmp_path)
    store.save_file(make_entry("ws1"), SHEET)

    store.delete_file("ws1", "wf_test")

    assert store.list_files("ws1") == ()

    with pytest.raises(WorkspaceError):
        store.load_parsed("ws1", "wf_test")


# ============================================================
# Isolation
# ============================================================


def test_files_are_invisible_from_another_workspace(tmp_path):
    store = WorkspaceStore(tmp_path)

    store.save_file(make_entry("ws1"), SHEET)

    assert store.list_files("ws2") == ()


def test_getting_another_workspaces_file_by_id_raises(tmp_path):
    """Knowing the file id must not be enough to read it."""

    store = WorkspaceStore(tmp_path)
    store.save_file(make_entry("ws1"), SHEET)

    with pytest.raises(WorkspaceError, match="No file"):
        store.get_file("ws2", "wf_test")


def test_loading_another_workspaces_parsed_content_raises(tmp_path):
    """
    [claude] load_parsed() resolves through the registry before touching the
    filesystem. Without that, a caller who knew a file id could read parsed
    content out of a directory it does not own — the path would simply not
    exist for their workspace, and any refactor that made the layout flatter
    would turn that into a real leak.
    """

    store = WorkspaceStore(tmp_path)
    store.save_file(make_entry("ws1"), SHEET)

    with pytest.raises(WorkspaceError):
        store.load_parsed("ws2", "wf_test")


def test_two_workspaces_keep_separate_registries(tmp_path):
    store = WorkspaceStore(tmp_path)

    store.save_file(make_entry("ws1", "wf_a"), SHEET)
    store.save_file(make_entry("ws2", "wf_b"), SHEET)

    assert [f.file_id for f in store.list_files("ws1")] == ["wf_a"]
    assert [f.file_id for f in store.list_files("ws2")] == ["wf_b"]


def test_traversal_in_a_workspace_id_never_reaches_the_filesystem(tmp_path):
    store = WorkspaceStore(tmp_path)

    with pytest.raises(WorkspaceError):
        store.list_files("../../etc")

    with pytest.raises(WorkspaceError):
        store.get_file("ws1/../ws2", "wf_test")


def test_raw_files_are_stored_under_the_file_id_not_the_filename(tmp_path):
    """
    A hostile filename must not become a path. It is recorded for display
    and never used to build one.
    """

    store = WorkspaceStore(tmp_path)

    entry = WorkspaceFile(
        file_id="wf_safe",
        workspace_id="ws1",
        filename="../../.env.csv",
        kind=FileKind.SPREADSHEET,
        byte_size=3,
        ingested_at=datetime.now(UTC),
    )

    store.save_file(entry, SHEET, source=b"a,b")

    written = list((tmp_path / "ws1" / "raw").iterdir())

    assert [path.name for path in written] == ["wf_safe.csv"]
    assert store.get_file("ws1", "wf_safe").filename == "../../.env.csv"
