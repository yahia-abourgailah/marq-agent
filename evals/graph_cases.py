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
    # Which domain graph to run the case against.
    domain: str = "deals"
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
    # ---- explicit limits must survive the handoff --------------------
    GraphCase(
        name="explicit_window_is_not_widened",
        question="How many deals are closing in the next 30 days?",
        answer_contains=("17",),
        answer_excludes=("96", "106"),
        max_tool_calls=2,
        why=(
            "The agent paraphrased this to 'closing soonest', dropped the "
            "30-day window and answered 96 — every future closing. An "
            "explicit limit the user gives is part of the question."
        ),
        tags=("wording",),
    ),
    GraphCase(
        name="subset_percentage_uses_the_right_denominator",
        question=(
            "How many contracted deals do we have, and what share of active "
            "deals is that?"
        ),
        # 91.84% is the number that distinguishes the two tools. Restating
        # the denominator is optional phrasing, so do not require it.
        answer_contains=("225", "91.8"),
        answer_excludes=("470", "47.8"),
        min_tool_calls=2,
        why=(
            "Contracted deals are a subset of active deals. Passing both to "
            "calculate_share sums them to 470 and reports 47.87%; the right "
            "tool is calculate_percentage, giving 91.84%."
        ),
        tags=("tools",),
    ),
    GraphCase(
        name="broad_question_is_answered_not_deflected",
        question="How is our business doing overall?",
        # 315 is the assertion that matters: it is the total, and the two
        # ways of getting this wrong are quoting 225 (contracted) or 245
        # (active) as the headline. Requiring the breakdown figures too only
        # adds sensitivity to phrasing.
        answer_contains=("315",),
        answer_excludes=("don't have access", "no general summary"),
        min_tool_calls=1,
        max_tool_calls=3,
        why=(
            "A vague business question is answerable: count the deals and "
            "split by status. The headline must equal the sum of the split "
            "(315), not the contracted count (225) or the active count (245)."
        ),
        tags=("analysis",),
    ),
    GraphCase(
        name="conversion_rate_counts_leads",
        question="What is our overall lead-to-deal conversion rate?",
        answer_contains=("31",),
        answer_excludes=("35.63", "315 deals"),
        max_tool_calls=2,
        why=(
            "Counting leads that produced a deal gives 31.67%. Dividing "
            "total deals by total leads gives 35.63% and double-counts any "
            "lead with more than one deal."
        ),
        tags=("analysis", "metrics"),
    ),
    GraphCase(
        name="group_rates_are_not_all_100_percent",
        question=(
            "Are commercial deals more likely to end up contracted than "
            "non-commercial ones?"
        ),
        answer_contains=("72", "68"),
        answer_excludes=("100%", "both"),
        max_tool_calls=2,
        why=(
            "Filtering by the measured condition made both rates 100%. The "
            "real figures are 72.18% non-commercial and 68.66% commercial, "
            "so the honest answer is 'no'."
        ),
        tags=("analysis", "metrics"),
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


# ---- leads domain ---------------------------------------------------

LEADS_GRAPH_CASES: tuple[GraphCase, ...] = (
    GraphCase(
        name="leads_stage_breakdown",
        domain="leads",
        question="How many leads are in each stage?",
        # Enumerating several stage ids is what separates a real breakdown
        # from a refusal. Noting that the names are unavailable alongside it
        # is correct and must not fail the case.
        answer_contains=("stage 1", "stage 2"),
        max_tool_calls=2,
        why=(
            "Stage names are unavailable but the ids are queryable, so this "
            "must produce a breakdown rather than a refusal."
        ),
        tags=("leads",),
    ),
    GraphCase(
        name="leads_named_stage_is_declined",
        domain="leads",
        question="How many leads are in the Hot Case stage?",
        answer_contains=("name",),
        why=(
            "Stage names cannot be mapped to ids. The agent should say the "
            "names are unavailable rather than guess an id."
        ),
        tags=("leads", "refusal"),
    ),
    GraphCase(
        name="leads_agent_declines_deals_questions",
        domain="leads",
        question="How many contracted deals do we have?",
        answer_excludes=("there are", "we have 2", "we have 3"),
        why=(
            "Deals are outside this domain. The agent must decline rather "
            "than report a number it cannot have obtained."
        ),
        tags=("leads", "isolation"),
    ),
    GraphCase(
        name="leads_source_share",
        domain="leads",
        question="What share of our leads comes from each utm source?",
        answer_contains=("%",),
        min_tool_calls=1,
        max_tool_calls=3,
        why=(
            "utm_source is real text, and shares come from calculate_share "
            "over the grouped counts."
        ),
        tags=("leads",),
    ),
    GraphCase(
        name="leads_explicit_window_survives",
        domain="leads",
        question="How many leads were created in the last 60 days?",
        answer_contains=("69",),
        answer_excludes=("884",),
        max_tool_calls=2,
        why="Dropping the window would silently answer for all leads.",
        tags=("leads", "wording"),
    ),
)


GRAPH_CASES = GRAPH_CASES + LEADS_GRAPH_CASES


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


def graph_for(domain_name: str):
    """[claude] Build the compiled graph for one domain."""

    from app.graph.agents.domain import DOMAINS
    from app.graph.builder import build_graph

    return build_graph(DOMAINS[domain_name])


async def main_async() -> int:
    graphs = {name: graph_for(name) for name in {c.domain for c in GRAPH_CASES}}
    failures = []

    for case in GRAPH_CASES:
        ok, detail = await evaluate(case, graphs[case.domain])
        print(f"  {'PASS' if ok else 'FAIL'}  [{case.domain}] {case.name}")
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
