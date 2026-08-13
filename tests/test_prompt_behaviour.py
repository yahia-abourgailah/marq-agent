"""
[claude] Runs the eval cases as tests, so a prompt or catalogue edit that
changes agent behaviour fails CI instead of being noticed in Studio.

Needs the live model endpoint, so it is marked integration:

    pytest -m integration tests/test_prompt_behaviour.py

The same cases are runnable as a report with `python -m evals.run`.
"""

from __future__ import annotations

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
