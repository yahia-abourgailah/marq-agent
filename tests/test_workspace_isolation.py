"""
[claude] Isolation under pressure, and concurrency.

Isolation is asserted elsewhere for the simple case. These push on it: many
workspaces at once, ids guessed across boundaries, and simultaneous writes
to one workspace's registry.

The concurrency cases matter now rather than later. Ingestion is synchronous
and the registry is a read-modify-write of a single JSON file, which is
correct for one caller at a time and a lost-update race for two. Nothing is
concurrent today because there is no HTTP layer — which is exactly why this
is the moment to find out.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.workspace.index import build_index
from app.workspace.ingest import ingest_file
from app.workspace.service import WorkspaceService
from app.workspace.store import WorkspaceError, WorkspaceStore
from tests.workspace_support import FakeEmbedder


@pytest.fixture
def service(tmp_path):
    embedder = FakeEmbedder()

    return WorkspaceService(
        store=WorkspaceStore(tmp_path / "store"),
        index=build_index(collection="isolation", dimension=embedder.dimension),
        embedder=embedder,
    )


def upload(service, workspace, name, body):
    return service.ingest(workspace, name, body.encode())


# ============================================================
# Many workspaces at once
# ============================================================


def test_twenty_workspaces_never_see_each_other(service):
    for index in range(20):
        upload(
            service,
            f"tenant-{index}",
            "data.csv",
            f"id,secret\n{index},value-{index}\n",
        )

    for index in range(20):
        files = service.list_files(f"tenant-{index}")

        assert len(files) == 1

        rows = service.read_rows(f"tenant-{index}", files[0].file_id)

        assert rows["rows"] == [{"id": str(index), "secret": f"value-{index}"}]


def test_search_never_crosses_twenty_workspaces(service):
    for index in range(20):
        upload(
            service,
            f"tenant-{index}",
            "notes.csv",
            f"note\nconfidential alpha figures for tenant {index}\n",
        )

    for index in range(20):
        hits, _ = service.search(
            f"tenant-{index}", "confidential alpha figures", limit=50
        )

        assert hits, "the tenant's own content should be findable"
        assert all(h.chunk.workspace_id == f"tenant-{index}" for h in hits)


def test_a_file_id_from_another_workspace_is_useless(service):
    upload(service, "owner", "secret.csv", "id,amount\n1,999999\n")

    stolen = service.list_files("owner")[0].file_id

    upload(service, "attacker", "mine.csv", "id\n1\n")

    for call in (
        lambda: service.get_file("attacker", stolen),
        lambda: service.load_parsed("attacker", stolen),
        lambda: service.read_rows("attacker", stolen),
        lambda: service.aggregate("attacker", stolen, operation="count"),
        lambda: service.search("attacker", "amount", file_id=stolen),
    ):
        with pytest.raises(WorkspaceError):
            call()


def test_deleting_from_one_workspace_does_not_touch_another(service):
    upload(service, "a", "shared_name.csv", "id\n1\n")
    upload(service, "b", "shared_name.csv", "id\n2\n")

    target = service.list_files("a")[0].file_id
    service.delete("a", target)

    assert service.list_files("a") == ()
    assert len(service.list_files("b")) == 1

    rows = service.read_rows("b", service.list_files("b")[0].file_id)
    assert rows["rows"] == [{"id": "2"}]


# ============================================================
# Concurrency
# ============================================================


def test_concurrent_uploads_to_one_workspace_do_not_lose_entries(service):
    """
    [claude] The registry is rewritten whole on every save, so two callers
    that read it at the same time and write it back in turn lose one entry.

    With no HTTP layer nothing is concurrent yet — but an upload endpoint is
    the very next thing planned, and a lost upload is invisible: the user is
    told their file was accepted and it is simply not in the list.
    """

    names = [f"file_{index}.csv" for index in range(12)]

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(
            pool.map(
                lambda name: upload(service, "busy", name, "id,amount\n1,10\n"),
                names,
            )
        )

    stored = {entry.filename for entry in service.list_files("busy")}

    missing = set(names) - stored

    assert not missing, (
        f"{len(missing)} of {len(names)} uploads were lost to a registry "
        f"race: {sorted(missing)}"
    )


def test_concurrent_uploads_to_different_workspaces_are_unaffected(service):
    """Separate directories, so these cannot contend even in principle."""

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(
            pool.map(
                lambda index: upload(
                    service, f"ws-{index}", "a.csv", "id\n1\n"
                ),
                range(12),
            )
        )

    for index in range(12):
        assert len(service.list_files(f"ws-{index}")) == 1


def test_reading_while_writing_never_sees_a_half_written_registry(service):
    """
    A reader must get either the old registry or the new one, never a
    truncated file — which would surface as a JSON decode error mid-answer.
    """

    upload(service, "busy", "first.csv", "id\n1\n")

    errors: list[Exception] = []

    def reader():
        for _ in range(60):
            try:
                service.list_files("busy")
            except Exception as exc:  # noqa: BLE001 - recording, not handling
                errors.append(exc)

    def writer():
        for index in range(12):
            upload(service, "busy", f"w{index}.csv", "id\n1\n")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(reader) for _ in range(3)]
        futures.append(pool.submit(writer))
        for future in futures:
            future.result()

    assert not errors, f"readers saw a broken registry: {errors[:3]}"


# ============================================================
# The tool boundary
# ============================================================


def test_ingest_requires_a_valid_workspace_before_touching_disk(tmp_path):
    store = WorkspaceStore(tmp_path / "store")

    with pytest.raises(WorkspaceError):
        ingest_file(
            store=store,
            index=None,
            embedder=None,
            workspace_id="../escape",
            filename="a.csv",
            data=b"id\n1\n",
        )

    # Nothing was created anywhere under the root.
    assert not list(store.root.rglob("*.csv"))
