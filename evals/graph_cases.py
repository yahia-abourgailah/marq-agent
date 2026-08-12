"""
[claude] Behavioural cases for the whole graph, not just the SQL Agent.

evals/cases.py checks the SQL the SQL Agent writes. It cannot see the Deals
Agent, and two real bugs lived exactly there:

  - it paraphrased "closing soonest" into "earliest closing dates", a
    different question, and answered with deals that closed years ago
  - it asked the user to clarify "the total contract price" instead of
    calling sql_query and reporting that the data is restricted

Both were invisible to the SQL-level eval and to the test suite. These cases
assert on tool usage and the final answer.

    python -m evals.graph_cases
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass(frozen=True)
class GraphCase:
    name: str
    question: str
    # Lowercased substrings that must all appear in the final answer.
    answer_contains: tuple[str, ...] = ()
    answer_excludes: tuple[str, ...] = ()
    min_tool_calls: int = 1
    max_tool_calls: int = 4
    why: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


GRAPH_CASES: tuple[GraphCase, ...] = (
    GraphCase(
        name="simple_count",
        question="How many active deals do we have?",
        answer_contains=("active",),
        max_tool_calls=1,
        why="One question, one query.",
        tags=("basic",),
    ),
    GraphCase(
        name="chains_into_analysis_tool",
        question=(
            "How many contracted deals, and what percentage of active "
            "deals is that?"
        ),
        answer_contains=("%",),
        min_tool_calls=2,
        why="Retrieval then arithmetic — sql_query into calculate_percentage.",
        tags=("basic",),
    ),
    GraphCase(
        name="restricted_data_is_reported_not_clarified",
        question="What is the total contract price?",
        answer_contains=("not available",),
        answer_excludes=("could you", "which deals", "can you specify", "?"),
        min_tool_calls=1,
        max_tool_calls=1,
        why=(
            "The agent must call sql_query and relay the refusal. Asking the "
            "user to narrow scope is wrong: the limit is on the data, so no "
            "clarification helps."
        ),
        tags=("refusal",),
    ),
    GraphCase(
        name="temporal_wording_survives_the_handoff",
        question="Show me the 5 deals closing soonest.",
        answer_excludes=("2024", "2025"),
        why=(
            "'Soon' means upcoming. Paraphrasing it to 'earliest' loses that "
            "and returns deals that closed years ago."
        ),
        tags=("wording",),
    ),
    GraphCase(
        name="invented_status_is_reported_not_guessed",
        question="How many deals are in negotiation?",
        answer_excludes=("0 deals", "there are 0"),
        why=(
            "No negotiation status exists. Saying so beats reporting a "
            "confident zero from a guessed filter."
        ),
        tags=("refusal",),
    ),
)


async def evaluate(case: GraphCase, graph) -> tuple[bool, str]:
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": case.question}]},
        config={"configurable": {"thread_id": f"eval-{case.name}"}},
    )

    messages = result["messages"]
    tool_calls = sum(1 for m in messages if getattr(m, "type", "") == "tool")
    answer = str(messages[-1].content)
    lowered = answer.lower()

    problems = []

    if not case.min_tool_calls <= tool_calls <= case.max_tool_calls:
        problems.append(
            f"{tool_calls} tool calls, expected "
            f"{case.min_tool_calls}-{case.max_tool_calls}"
        )

    missing = [s for s in case.answer_contains if s not in lowered]
    if missing:
        problems.append(f"answer missing {missing}")

    present = [s for s in case.answer_excludes if s in lowered]
    if present:
        problems.append(f"answer must not contain {present}")

    if problems:
        return False, f"{'; '.join(problems)}\n      answer: {answer[:200]}"

    return True, f"[{tool_calls} call] {answer[:120]}"


async def main_async() -> int:
    from app.graph.builder import build_graph

    graph = build_graph()
    failures = []

    for case in GRAPH_CASES:
        ok, detail = await evaluate(case, graph)
        print(f"  {'PASS' if ok else 'FAIL'}  {case.name}")
        if ok:
            print(f"        {detail}")
        else:
            failures.append(case.name)
            print(f"        why: {case.why}")
            print(f"        {detail}")

    print(f"\n  {len(GRAPH_CASES) - len(failures)}/{len(GRAPH_CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
