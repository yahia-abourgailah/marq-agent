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
from evals.run import evaluate

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def sql_agent():
    from app.llm.model import get_model
    from app.sql.agent import build_sql_agent

    return build_sql_agent(get_model())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_case(case, sql_agent):
    passed, detail = await evaluate(case, sql_agent)

    assert passed, f"{case.why}\n{detail}"


# ============================================================
# [claude] Graph-level cases.
#
# The SQL-level cases above cannot see the Deals Agent. Paraphrasing and
# premature clarification bugs both lived there and were invisible to them.
# ============================================================


@pytest.fixture(scope="module")
def graph():
    from app.graph.builder import build_graph

    return build_graph()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", GRAPH_CASES, ids=lambda c: c.name)
async def test_graph_case(case, graph):
    passed, detail = await graph_evaluate(case, graph)

    assert passed, f"{case.why}\n{detail}"
