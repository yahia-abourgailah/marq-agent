"""
[claude] Runs the eval cases as tests, so a prompt or catalogue edit that
changes agent behaviour fails CI instead of being noticed in Studio.

Needs the live model endpoint, so it is marked integration:

    pytest -m integration tests/test_prompt_behaviour.py

The same cases are runnable as a report with `python -m evals.run`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evals.cases import CASES
from evals.graph_cases import GRAPH_CASES
from evals.graph_cases import evaluate as graph_evaluate
from evals.routing_cases import ROUTE_CASES
from evals.routing_cases import evaluate as route_evaluate
from evals.run import evaluate

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def sql_agents():
    """[claude] One SQL Agent per domain — cases are domain-scoped."""

    from evals.run import sql_agent_for

    return {name: sql_agent_for(name) for name in {c.domain for c in CASES}}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_case(case, sql_agents):
    passed, detail = await evaluate(case, sql_agents[case.domain])

    assert passed, f"{case.why}\n{detail}"


# ============================================================
# [claude] Graph-level cases.
#
# The SQL-level cases above cannot see the Deals Agent. Paraphrasing and
# premature clarification bugs both lived there and were invisible to them.
# ============================================================


@pytest.fixture(scope="module")
def graphs():
    """[claude] One compiled graph per domain — cases are domain-scoped."""

    from evals.graph_cases import graph_for

    return {name: graph_for(name) for name in {c.domain for c in GRAPH_CASES}}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", GRAPH_CASES, ids=lambda c: c.name)
async def test_graph_case(case, graphs):
    passed, detail = await graph_evaluate(case, graphs[case.domain])

    assert passed, f"{case.why}\n{detail}"


# ============================================================
# [claude] Supervisor routing.
#
# A misroute is quiet: the Leads Agent asked a deals question declines
# politely rather than erroring, so nothing else in the suite would catch it.
# ============================================================


@pytest.fixture(scope="module")
def routing_model():
    from app.llm.model import get_model

    return get_model()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ROUTE_CASES, ids=lambda c: c.question[:40])
async def test_routing_case(case, routing_model):
    correct, got = await route_evaluate(case, routing_model)

    assert correct, (
        f"routed to {got}, expected {case.expected}"
        + (f" — {case.why}" if case.why else "")
    )


# ============================================================
# [claude] Workspace Agent behaviour.
#
# These need a built workspace, so the fixture uploads real files into a
# temporary directory and an in-memory index, and the graph is built with
# that service injected. Nothing touches whatever is uploaded locally.
#
# They exist because the two properties holding the workspace design up —
# totals come from workspace_aggregate rather than workspace_search, and a
# comparison reports what the tool found — are invisible to every other
# suite. The hermetic tests prove the tools are right; only these check that
# the agent reaches for the right one.
# ============================================================


@pytest.fixture(scope="module")
def workspace_setup():
    """
    Build the eval workspace once, and the graph that reads it.

    [claude] `asyncio.run` rather than `get_event_loop().run_until_complete`,
    which raises outside a running loop on Python 3.12+ and would have failed
    the first time anyone ran this suite. Caught by the consistency audit
    hitting the same mistake.
    """

    import asyncio

    from app.graph.agents.domain import WORKSPACE
    from app.graph.builder import build_graph
    from evals.workspace_cases import build_cases
    from evals.workspace_fixture import build

    expected = asyncio.run(build())
    service = expected.pop("service")

    return build_cases(expected), build_graph(
        WORKSPACE, workspace_service=service
    )


@pytest.mark.asyncio
async def test_workspace_cases(workspace_setup):
    """
    [claude] Run every workspace case against one built workspace.

    Not parametrised, unlike the suites above: building the workspace means
    loading the embedding model and ingesting two files, and pytest would
    tear the module fixture down and rebuild it per parameter under some
    loop scopes. One test, all cases, and the assertion names whichever
    failed.
    """

    cases, graph = workspace_setup

    failures = []

    for case in cases:
        passed, detail = await graph_evaluate(case, graph)

        if not passed:
            failures.append(f"{case.name}: {detail}\n  why: {case.why}")

    assert not failures, "\n\n".join(failures)


# ============================================================
# Chunks must fit the encoder that will embed them
# ============================================================


@pytest.mark.integration
def test_no_chunk_exceeds_the_encoder_input_window():
    """
    [claude] From the 24 August review, which asked for exactly this
    assertion and was right to.

    `max_seq_length` is enforced by truncation, not by an error. An
    oversized chunk therefore fails silently: nothing raises, the vector is
    written, and the tail of the text was simply never searchable. The old
    constants — 700 characters and 20 rows — were justified by a comment
    that named the right window and got the conversion wrong, and no test
    could have noticed.

    This loads the real tokenizer and measures, over both lanes and over
    Arabic as well as English, because the encoder was chosen for a CRM
    that carries Arabic and XLM-R segments it far more aggressively.
    """

    from app.workspace.chunking import MAX_CHUNK_TOKENS, build_chunks
    from app.workspace.embeddings import SentenceTransformerEmbedder
    from app.workspace.models import (
        ColumnSpec,
        FileKind,
        PageContent,
        ParsedFile,
        SheetContent,
        WorkspaceFile,
    )

    encoder = SentenceTransformerEmbedder()

    entry = WorkspaceFile(
        file_id="wf_probe",
        workspace_id="ws_probe",
        filename="probe",
        kind=FileKind.DOCUMENT,
        byte_size=1,
        ingested_at=datetime.now(UTC),
    )

    english = (
        "The franchise reported a cancellation rate of 34.62 percent across "
        "twenty six deals this quarter, materially above the company average "
        "measured across all three hundred and fifteen deals on record. "
    ) * 12
    arabic = (
        "تقرير عن معدل الإلغاء في الامتياز التجاري خلال الربع الحالي مقارنة "
        "بالمتوسط العام للشركة عبر جميع الصفقات المسجلة في النظام حتى تاريخه. "
    ) * 12

    columns = [f"column_{i}" for i in range(9)]
    sheet = SheetContent(
        name="Q3 Tracker",
        columns=tuple(ColumnSpec(c, "text", 60) for c in columns),
        rows=tuple(
            {c: f"{c}-value-{r}-A1204" for c in columns} for r in range(60)
        ),
    )

    cases = {
        "english document": ParsedFile(
            kind=FileKind.DOCUMENT,
            pages=(PageContent(number=1, text=english),),
        ),
        "arabic document": ParsedFile(
            kind=FileKind.DOCUMENT,
            pages=(PageContent(number=1, text=arabic),),
        ),
        "wide spreadsheet": ParsedFile(
            kind=FileKind.SPREADSHEET, sheets=(sheet,)
        ),
    }

    for label, parsed in cases.items():
        chunks = build_chunks(
            entry, parsed, count_tokens=encoder.count_tokens
        )

        assert chunks, f"{label} produced no chunks"

        for chunk in chunks:
            pieces = encoder.count_tokens(chunk.text)

            assert pieces <= MAX_CHUNK_TOKENS, (
                f"{label}: a chunk is {pieces} word-pieces against a "
                f"{MAX_CHUNK_TOKENS} window — its tail will be truncated "
                "before it is embedded, silently"
            )
