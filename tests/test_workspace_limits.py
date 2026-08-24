"""
[claude] Behaviour exactly at the limits, and just past them.

Caps are only useful if crossing one is *visible*. A silently truncated
result is worse than no cap at all: the agent reports a total over the part
it happened to receive, and nothing anywhere says the answer is partial.

So each of these checks two things — the cap holds, and the payload says it
held.
"""

from __future__ import annotations

import pytest

from app.tools.workspace import MAX_COMPARE_ROWS, build_workspace_tools
from app.workspace.compare import MAX_EXAMPLES, compare_rows
from app.workspace.index import build_index
from app.workspace.query import MAX_RETURNED_ROWS, Filter, aggregate
from app.workspace.readers import read_spreadsheet
from app.workspace.service import WorkspaceService
from app.workspace.store import WorkspaceStore
from tests.workspace_support import FakeEmbedder


@pytest.fixture
def service(tmp_path):
    embedder = FakeEmbedder()

    return WorkspaceService(
        store=WorkspaceStore(tmp_path / "store"),
        index=build_index(collection="limits", dimension=embedder.dimension),
        embedder=embedder,
    )


def sheet_with(rows: int, tmp_path):
    lines = ["id,amount"]
    lines += [f"{i},{i}" for i in range(1, rows + 1)]

    path = tmp_path / "big.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return path


# ============================================================
# Row reads
# ============================================================


def test_a_read_at_the_cap_is_flagged_as_truncated(service, tmp_path):
    service.ingest_path("ws", sheet_with(MAX_RETURNED_ROWS + 50, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=MAX_RETURNED_ROWS + 50)

    assert result["matched_rows"] == MAX_RETURNED_ROWS + 50
    assert result["returned_rows"] == MAX_RETURNED_ROWS
    assert result["truncated"] is True


def test_a_read_just_under_the_cap_is_not_flagged(service, tmp_path):
    service.ingest_path("ws", sheet_with(MAX_RETURNED_ROWS - 1, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=MAX_RETURNED_ROWS)

    assert result["truncated"] is False
    assert result["returned_rows"] == MAX_RETURNED_ROWS - 1


def test_exactly_at_the_cap_is_not_falsely_flagged(service, tmp_path):
    """An off-by-one here would tell the user a complete answer is partial."""

    service.ingest_path("ws", sheet_with(MAX_RETURNED_ROWS, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=MAX_RETURNED_ROWS)

    assert result["returned_rows"] == MAX_RETURNED_ROWS
    assert result["truncated"] is False


def test_a_caller_cannot_raise_the_cap(service, tmp_path):
    service.ingest_path("ws", sheet_with(MAX_RETURNED_ROWS + 100, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=10_000)

    assert result["returned_rows"] == MAX_RETURNED_ROWS
    assert result["truncated"] is True


@pytest.mark.parametrize("limit", [0, -1, -999])
def test_a_nonsense_limit_still_returns_something_sane(service, tmp_path, limit):
    service.ingest_path("ws", sheet_with(10, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows("ws", file_id, limit=limit)

    assert result["returned_rows"] >= 1


# ============================================================
# Aggregates ignore the read cap
# ============================================================


def test_an_aggregate_covers_every_row_not_just_the_readable_ones(
    service, tmp_path
):
    """
    The property the whole design rests on: reads are capped, totals are
    not. If aggregation stopped at the read cap, a big file would silently
    report the sum of its first 200 rows.
    """

    count = MAX_RETURNED_ROWS + 300
    service.ingest_path("ws", sheet_with(count, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.aggregate("ws", file_id, operation="sum", column="amount")

    assert result["matched_rows"] == count
    assert result["value"] == count * (count + 1) / 2


def test_a_grouped_aggregate_also_covers_everything(service, tmp_path):
    count = MAX_RETURNED_ROWS + 300
    service.ingest_path("ws", sheet_with(count, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.aggregate(
        "ws", file_id, operation="count", group_by="amount"
    )

    assert result["matched_rows"] == count


# ============================================================
# Empty and degenerate inputs
# ============================================================


def test_a_filter_matching_nothing_reports_zero_not_an_error(
    service, tmp_path
):
    service.ingest_path("ws", sheet_with(10, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    result = service.read_rows(
        "ws", file_id, filters=[Filter("amount", "gt", 10**9)]
    )

    assert result["matched_rows"] == 0
    assert result["rows"] == []
    assert result["truncated"] is False


def test_a_csv_sheet_is_named_after_the_upload_not_the_storage_id(
    service, tmp_path
):
    """
    [claude] A delimited file borrows its sheet name from the filename, and
    on disk that is the opaque id — so the agent told users their data lived
    in "sheet wf_21b07b7fe4281016". Found by reading a failing test's dump.
    """

    (tmp_path / "Q3 Tracker.csv").write_text("id\n1\n", encoding="utf-8")
    service.ingest_path("ws", tmp_path / "Q3 Tracker.csv")

    entry = service.list_files("ws")[0]

    assert entry.sheets[0].name == "Q3 Tracker"


def test_passing_raw_dicts_as_filters_says_what_to_do_instead(
    service, tmp_path
):
    """The old failure was an AttributeError several frames deep."""

    from app.workspace.query import QueryError

    service.ingest_path("ws", sheet_with(3, tmp_path))
    file_id = service.list_files("ws")[0].file_id

    with pytest.raises(QueryError, match="Filter objects"):
        service.read_rows(
            "ws", file_id, filters=[{"column": "id", "op": "eq", "value": 1}]
        )


def test_a_single_row_single_column_file_works(service, tmp_path):
    (tmp_path / "tiny.csv").write_text("only\n1\n", encoding="utf-8")
    service.ingest_path("ws", tmp_path / "tiny.csv")

    file_id = service.list_files("ws")[0].file_id
    result = service.aggregate("ws", file_id, operation="sum", column="only")

    assert result["value"] == 1


def test_averaging_nothing_returns_none_rather_than_dividing_by_zero(
    tmp_path,
):
    (tmp_path / "a.csv").write_text("id,amount\n1,10\n", encoding="utf-8")
    sheet = read_spreadsheet(tmp_path / "a.csv")[0].sheets[0]

    result = aggregate(
        sheet, "avg", column="amount", filters=[Filter("id", "eq", 999)]
    )

    assert result["value"] is None
    assert result["matched_rows"] == 0


# ============================================================
# Comparison limits
# ============================================================


def test_comparison_counts_stay_exact_far_past_the_example_cap():
    left = [{"id": i, "v": i} for i in range(1, 601)]
    right = [{"id": i, "v": i + 1} for i in range(1, 601)]

    result = compare_rows(left, right, key="id", value_columns=["v"])

    assert result["totals"]["mismatched"] == 600
    assert len(result["mismatched"]) == MAX_EXAMPLES


@pytest.mark.asyncio
async def test_the_compare_payload_ceiling_is_refused_not_silently_trimmed(
    tmp_path,
):
    """
    Trimming would reconcile part of the data and report it as the whole,
    which is the failure this tool exists to prevent.
    """

    service = WorkspaceService(store=WorkspaceStore(tmp_path))
    tools = {t.name: t for t in build_workspace_tools(service)}

    rows = [{"id": i} for i in range(MAX_COMPARE_ROWS + 1)]

    result = await tools["compare_with_crm"].coroutine(
        uploaded_rows=rows, crm_rows=[], key="id"
    )

    assert result["success"] is False
    assert result["retryable"] is True


@pytest.mark.asyncio
async def test_exactly_at_the_compare_ceiling_is_accepted(tmp_path):
    service = WorkspaceService(store=WorkspaceStore(tmp_path))
    tools = {t.name: t for t in build_workspace_tools(service)}

    rows = [{"id": i, "v": 1} for i in range(MAX_COMPARE_ROWS)]

    result = await tools["compare_with_crm"].coroutine(
        uploaded_rows=rows, crm_rows=rows, key="id", value_columns=["v"]
    )

    assert result["success"] is True
    assert result["totals"]["matched"] == MAX_COMPARE_ROWS


# ============================================================
# Search limits
# ============================================================


def test_search_on_an_empty_query_returns_nothing_rather_than_everything(
    service, tmp_path
):
    service.ingest_path("ws", sheet_with(5, tmp_path))

    # [claude] `search` returns the passages that cleared the relevance
    # floor and the number that did not — see MIN_RELEVANCE_SCORE.
    assert service.search("ws", "") == ([], 0)
    assert service.search("ws", "   ") == ([], 0)


def test_search_never_returns_more_than_asked(service, tmp_path):
    service.ingest_path("ws", sheet_with(400, tmp_path))

    hits, _ = service.search("ws", "amount", limit=3)

    assert len(hits) <= 3


# ============================================================
# The relevance floor
# ============================================================


def test_an_unrelated_question_returns_nothing_rather_than_the_nearest_thing(
    service, tmp_path
):
    """
    [claude] From the 24 August review.

    `min_score` defaulted to zero and no caller raised it, so every search
    returned its eight nearest neighbours whatever was asked. A workspace
    holding a unit schedule, asked about staffing policy, returned eight
    passages about units — scored around 0.1 and formatted exactly like
    passages that answer the question.

    The handoff had recorded an earlier version of this expectation as
    wrong: "it returns the nearest neighbours regardless — that is the
    whole point". Right about the mechanism, wrong about what to do with
    it. The nearest neighbour to an unrelated question is noise.

    The fake embedder is a token hash, so this asserts the *plumbing* —
    the floor is read off the encoder, applied, and the drops counted.

    It deliberately does not assert that a topical question survives 0.25,
    because measured against the fake an unrelated query scores 0.45 and a
    topical one scores 0.04. Its scores carry no meaning, which is exactly
    why the floor is a property of the encoder and why the fake declares
    none. Whether 0.25 separates signal from noise is a question about the
    real model, answered by measurement and recorded on
    `SentenceTransformerEmbedder.min_relevance_score`.
    """

    service.ingest_path("ws", sheet_with(20, tmp_path))

    # The fake declares no floor, so nothing is dropped on its account.
    assert getattr(service.embedder, "min_relevance_score", None) is None

    hits, below = service.search("ws", "amount", limit=8)

    assert hits, "with no declared floor, retrieval is unfiltered"
    assert below == 0


def test_the_dropped_count_is_reported_not_swallowed(service, tmp_path):
    """
    "No results" and "results, all too weak" need different sentences from
    the agent — the second is "your files do not cover this", which is a
    useful answer rather than a shrug.
    """

    service.ingest_path("ws", sheet_with(20, tmp_path))

    # An encoder declaring a floor nothing can clear: every neighbour
    # becomes a drop, and the count has to say so.
    real = service.embedder

    class Impossible:
        """Wraps the real fake, and declares an unreachable floor."""

        min_relevance_score = 2.0

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

    service.embedder = Impossible(real)
    try:
        hits, below = service.search("ws", "amount", limit=8)
    finally:
        service.embedder = real

    assert hits == []
    assert below > 0, "passages were dropped but the count did not say so"


# ============================================================
# Identifiers take the exact path, not the vector one
# ============================================================


def test_identifier_shapes_are_recognised_and_prose_is_not():
    """
    [claude] The matcher is narrow on purpose. A false positive costs a
    full scan of every uploaded sheet for a token that was never an id —
    and "deals in 2026" scanning for 2026 would return every row carrying
    that year, which is worse than the search it displaced.
    """

    from app.tools.workspace import identifier_tokens

    assert identifier_tokens("what about unit A-1204?") == ["A-1204"]
    assert identifier_tokens("look up DEAL1042") == ["DEAL1042"]
    assert identifier_tokens("row 1204-88 please") == ["1204-88"]
    assert identifier_tokens("check #4471") == ["4471"]
    assert identifier_tokens("find 100004471") == ["100004471"]

    # Prose, years and small numbers are not identifiers.
    assert identifier_tokens("how many deals are contracted") == []
    assert identifier_tokens("how many deals in 2026") == []
    assert identifier_tokens("the top 5 projects") == []
    assert identifier_tokens("") == []


def test_at_most_three_identifiers_are_scanned():
    """Each one is a scan of every sheet; a question may not trigger many."""

    from app.tools.workspace import identifier_tokens

    question = "compare A-1 A-1204 B-2048 C-3072 D-4096 E-5120"

    assert len(identifier_tokens(question)) <= 3


def test_an_exact_identifier_match_beats_similarity(service, tmp_path):
    """
    [claude] The case R3 is about.

    `A-1204` and `A-1240` are near-neighbours to a paraphrase encoder, and
    neither reliably outranks a row that is merely *about* units. The exact
    path returns the row that actually contains the value.
    """

    csv = tmp_path / "units.csv"
    csv.write_text(
        "unit,client,status\n"
        "A-1204,Ibrahim,contracted\n"
        "A-1240,Farouk,reserved\n"
        "A-1024,Nadia,cancelled\n"
    )
    service.ingest_path("ws", csv)

    found = service.find_identifier("ws", "A-1204")

    assert len(found) == 1
    assert found[0]["row"]["client"] == "Ibrahim"
    assert "row 1" in found[0]["source"]

    # The near-neighbours are not returned; only the exact value is.
    assert service.find_identifier("ws", "A-1240")[0]["row"]["client"] == "Farouk"
    assert service.find_identifier("ws", "A-9999") == []


def test_identifier_lookup_is_case_and_space_insensitive(service, tmp_path):
    """
    Someone pasting an id out of an email will not match stored casing, and
    an exact-match tool that is exact about the wrong things is worse than
    none.
    """

    csv = tmp_path / "units.csv"
    csv.write_text("unit,client\nA-1204,Ibrahim\n")
    service.ingest_path("ws", csv)

    assert service.find_identifier("ws", "a-1204")
    assert service.find_identifier("ws", "  A-1204  ")


def test_identifier_lookup_does_not_cross_workspaces(service, tmp_path):
    """The tenancy boundary holds on the exact path too, not just the vector one."""

    csv = tmp_path / "units.csv"
    csv.write_text("unit,client\nA-1204,Ibrahim\n")
    service.ingest_path("owner", csv)

    assert service.find_identifier("owner", "A-1204")
    assert service.find_identifier("attacker", "A-1204") == []
