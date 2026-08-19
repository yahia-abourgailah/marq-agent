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
import re
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

    # [claude] Which uploaded files the turn may read. The workspace agent
    # needs one; every other domain ignores it.
    workspace_id: str | None = None

    # [claude] Assertions on *which* tools ran, not just how many.
    #
    # Counting calls cannot express the property that matters most in the
    # workspace: that a total came from workspace_aggregate rather than from
    # workspace_search. Both are one call, and only one of them is right —
    # a number recalled from retrieved passages is a sample presented as a
    # fact, and it looks entirely reasonable in the answer.
    tools_include: tuple[str, ...] = ()
    tools_exclude: tuple[str, ...] = ()

    # [claude] A scalar query whose result must appear in the answer.
    #
    # Added because two cases hardcoded row counts and went stale: the
    # fixture pins its rows at load time while CURRENT_DATE keeps moving, so
    # "69 leads in the last 60 days" quietly became 62 and the eval started
    # failing a correct answer. Deriving the expectation from the same
    # database the agent queried keeps the assertion honest as the fixture
    # ages — the eval-side form of the rule against putting database values
    # in prompts.
    expected_from_sql: str | None = None


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
        # [claude] Was the literal "17". The fixture emits dates as
        # CURRENT_DATE ± INTERVAL at load time, so its rows are pinned while
        # CURRENT_DATE keeps moving — the window slides and the literal goes
        # stale, failing a correct answer. Derived from the database instead.
        expected_from_sql=(
            "SELECT count(*) FROM deals "
            "WHERE deleted_at IS NULL "
            "AND expected_closing_date BETWEEN CURRENT_DATE "
            "AND CURRENT_DATE + INTERVAL '30 days'"
        ),
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
        # [claude] Was "31" (31.67%). Now 32.19%, because the corrected
        # merged-duplicate rule excludes merged leads from both sides —
        # 253 distinct converting leads out of 786 unique ones, rather than
        # 280 out of 884 where 98 of those leads are duplicates of each
        # other.
        #
        # The property under test is unchanged and still the point: the
        # numerator counts leads that produced a deal, never deals. 35.63%
        # remains excluded, so an answer built from deal counts still fails.
        answer_contains=("32",),
        answer_excludes=("35.63", "315 deals"),
        max_tool_calls=2,
        why=(
            "Counting leads that produced a deal gives 32.19%. Dividing "
            "total deals by total leads gives 35.63% and double-counts any "
            "lead with more than one deal — and merged duplicates are not "
            "separate leads, so they are excluded from both sides."
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
        name="deals_have_no_staleness_to_report",
        question="Which deals are stale?",
        # [claude] This assertion has now been wrong twice, in both
        # directions, which is the lesson worth keeping: the refusal's
        # *wording* moved as the catalogue rule improved, while the property
        # never did. Anchor on the substance — staleness belongs to leads —
        # and on the absence of an invented per-deal figure.
        answer_contains=("lead",),
        # `is_stale` exists on leads and has no deals equivalent. The agent
        # once answered this by deriving a staleness rule from date columns
        # and reporting it as a CRM metric — a fabricated measure is worse
        # than a refusal, because the user cannot tell.
        answer_excludes=("stale deals are", "days since", "no activity in"),
        max_tool_calls=2,
        why=(
            "Deals have no staleness concept. Saying so is the answer; "
            "deriving one from dates invents a metric the CRM does not have."
        ),
        tags=("refusal", "invention"),
    ),
    GraphCase(
        name="vague_franchise_question_uses_real_metrics",
        question="Which franchises should we worry about?",
        answer_contains=("franchise",),
        # [claude] Was answer_excludes=("stale",), which failed a correct
        # answer once the invented metric was fixed. "Stale leads by
        # franchise" is real — leads genuinely carry is_stale, and franchise
        # 6 really does have 19 — and it is a good answer to a vague
        # question. Only the *deals* version was ever fabricated, so exclude
        # that and nothing more.
        answer_excludes=("stale deals",),
        min_tool_calls=1,
        # [claude] Deliberately loose. "Which franchises should we worry
        # about" is an open question and several queries is a reasonable way
        # to answer it — a run took five and was right. This case is about
        # what the agent invents, not how many calls it spends; tightening
        # the ceiling here only fails correct answers.
        max_tool_calls=6,
        why=(
            "A vague question must be answered from columns that exist — "
            "cancellations, status mix — not from an invented staleness "
            "measure. This is where the fabricated metric surfaced."
        ),
        tags=("invention",),
    ),
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
        name="leads_conversion_by_response_speed_is_a_rate",
        domain="leads",
        question="What is the conversion rate by response speed?",
        # The fast bucket's rate, derived rather than pinned.
        expected_from_sql=(
            "SELECT round(100.0 * count(converted_at) / count(*), 2) "
            "FROM leads "
            "WHERE deleted_at IS NULL "
            "AND response_time_minutes IS NOT NULL "
            "AND response_time_minutes <= 60"
        ),
        # [claude] The wrong-answer signature. Dividing each bucket's
        # conversions by all conversions gives shares that sum to 100% —
        # 1.28 / 98.72 here — which answers "where do conversions come from"
        # rather than "does responding faster convert better". The agent
        # reported exactly that shape before the catalogue rule landed.
        answer_excludes=("1.28", "98.72", "99.89", "0.11"),
        max_tool_calls=2,
        why=(
            "A rate per bucket, not a share of the converted. The "
            "denominator is the leads in that bucket."
        ),
        tags=("leads", "metrics", "denominator"),
    ),
    GraphCase(
        name="leads_explicit_window_survives",
        domain="leads",
        question="How many leads were created in the last 60 days?",
        # [claude] Was the literal "69", which is what a 63-day window gives
        # today — the fixture had aged three days past the count baked in
        # here, so the eval failed a correct answer of 62. See
        # explicit_window_is_not_widened above.
        # [claude] Now excludes merged duplicates, matching the corrected
        # LEADS_RULES_ONLY. The case still tests exactly what it always
        # tested — that the 60-day window survives the handoff, guarded by
        # the 884 exclusion below — but its baseline moved when the merged
        # rule stopped being a judgement call: 57 lead records were created,
        # belonging to 50 distinct people.
        expected_from_sql=(
            "SELECT count(*) FROM leads "
            "WHERE deleted_at IS NULL AND merged_into_id IS NULL "
            "AND created_at >= CURRENT_DATE - INTERVAL '60 days'"
        ),
        answer_excludes=("884",),
        max_tool_calls=2,
        why="Dropping the window would silently answer for all leads.",
        tags=("leads", "wording"),
    ),
)


GRAPH_CASES = GRAPH_CASES + LEADS_GRAPH_CASES


async def scalar(query: str) -> str:
    """[claude] Run a one-value query, for expectations derived at eval time."""

    from app.db.connection import app_db

    await app_db.connect()

    async with app_db.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query)
            row = await cursor.fetchone()

    # Dict row factory on the pool, so take the single value whatever it is
    # called rather than indexing by position.
    value = next(iter(row.values())) if hasattr(row, "values") else row[0]

    # Render whole numbers without a trailing .0, which is how the agent
    # will have written them.
    if isinstance(value, float) and value.is_integer():
        value = int(value)

    return str(value)


async def evaluate(case: GraphCase, graph) -> tuple[bool, str]:
    state: dict = {"messages": [{"role": "user", "content": case.question}]}

    if case.workspace_id:
        state["workspace_id"] = case.workspace_id

    result = await graph.ainvoke(
        state,
        config={"configurable": {"thread_id": f"eval-{case.name}"}},
    )

    messages = result["messages"]
    tool_calls = sum(1 for m in messages if getattr(m, "type", "") == "tool")

    used = [
        call["name"]
        for message in messages
        for call in getattr(message, "tool_calls", []) or []
    ]

    answer = str(messages[-1].content)
    lowered = answer.lower()

    # [claude] Agents write 4,665 where a case asserts 4665, and a thousands
    # separator is not a wrong answer. Matching against both forms stops
    # every numeric expectation from depending on how the model chose to
    # format it — this failed a correct answer the first time it ran.
    unseparated = re.sub(r"(?<=\d),(?=\d)", "", lowered)

    def present_in_answer(needle: str) -> bool:
        needle = needle.lower()
        return needle in lowered or needle in unseparated

    problems = []

    if not case.min_tool_calls <= tool_calls <= case.max_tool_calls:
        problems.append(
            f"{tool_calls} tool calls, expected "
            f"{case.min_tool_calls}-{case.max_tool_calls}"
        )

    absent = [name for name in case.tools_include if name not in used]
    if absent:
        problems.append(f"did not call {absent} (called {used})")

    banned = [name for name in case.tools_exclude if name in used]
    if banned:
        problems.append(f"must not call {banned} (called {used})")

    expected = list(case.answer_contains)

    if case.expected_from_sql:
        expected.append(await scalar(case.expected_from_sql))

    missing = [s for s in expected if not present_in_answer(s)]
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
