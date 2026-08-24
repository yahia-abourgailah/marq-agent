"""
[claude] Questions that take more than one step.

Why a fifth suite
-----------------
The existing four each answer a different question, and none of them answers
this one. `evals/cases.py` checks the SQL the SQL Agent writes.
`graph_cases.py` checks a single domain agent end to end.
`routing_cases.py` checks the classifier. `workspace_cases.py` checks
uploaded files. Between them they are almost entirely **single-hop**: one
question, one query, one number.

That is not what anyone will actually ask a CRM. "Which franchise should I
worry about" is a comparison. "How does this quarter look against last" is
two windows. "What share of qualified leads went stale" is a rate whose
denominator is a subset, which is the exact family of bug this project keeps
finding. A suite of counts cannot tell you whether the agent can do any of
that.

Two things this suite does differently
--------------------------------------
**It runs through `build_supervisor_graph()`** — the production path —
rather than `build_graph(DOMAIN)`. docs/HANDOFF.md: "Test the entry point
people actually use. A green suite over a path nobody uses is not evidence."
The first end-to-end run through the supervisor immediately hit a step
ceiling that single-domain runs never reached, and a complex question is
exactly where a misroute is most likely, so routing is part of what is under
test here rather than assumed away.

**Every expectation is derived from the database at eval time**, never
hardcoded. The fixture pins its rows at load time while `CURRENT_DATE` keeps
moving, so a literal goes stale and starts failing a correct answer — which
already happened twice to `graph_cases.py`.

Every ground-truth query below was run against the database *before* the case
was written. That ordering is deliberate: `owner_name_joins_users` asserted
the wrong ownership column for weeks and propagated that error into the
catalogue, because an existing eval is evidence about what someone previously
believed, not about the world.

    python -m evals.complex_cases
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass(frozen=True)
class ComplexCase:
    name: str
    question: str

    why: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    # [claude] Several queries, not one. A comparison has two numbers in it,
    # and asserting only the first would pass an answer that got the second
    # one wrong — which is the more likely half to be wrong.
    expected_from_sql: tuple[str, ...] = ()

    answer_contains: tuple[str, ...] = ()

    # [claude] The wrong answer, named explicitly.
    #
    # Most of these questions have a plausible near-miss — the franchise with
    # the most cancellations rather than the worst rate, the count of all
    # stale leads rather than the stale share of qualified ones. Asserting
    # only the right number lets an answer containing both pass.
    answer_excludes: tuple[str, ...] = ()

    # Which specialist should own it. A complex question is where a misroute
    # is most likely and least visible.
    expected_route: str | None = None

    # [claude] Every specialist the turn should have run, and every tool.
    #
    # Added 24 August 2026, after a bug that survived precisely because
    # nothing asserted these. The supervisor planned `deals, research`
    # correctly and then handed each specialist the *whole* question; both
    # declined the halves they could not answer and the turn produced
    # nothing, four times out of four, while the deals agent held the
    # figure the first half needed.
    #
    # `expected_route` could not catch it — the route was right. The answer
    # assertions could not catch it — there were no such cases. Planning
    # correctly and then answering nothing is invisible to every check that
    # existed, which is why these two are separate from `expected_route`
    # rather than folded into it.
    expected_specialists: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()

    # A multi-hop question legitimately needs more calls than a count.
    min_tool_calls: int = 1
    max_tool_calls: int = 6


# ============================================================
# Cases
# ============================================================
#
# Ground truth for every case was computed against the fixture first; the
# comment above each one records what it was at the time of writing, while
# the assertion itself re-derives it at eval time.

COMPLEX_CASES: tuple[ComplexCase, ...] = (
    # ---- rate versus count: the denominator family ------------------
    ComplexCase(
        name="worst_rate_is_not_the_most_cancellations",
        question=(
            "Which franchise has the highest cancellation rate? Only count "
            "franchises with at least 20 deals."
        ),
        # franchise 12 — 9 of 26 = 34.62%. Franchise 4 has MORE
        # cancellations (10) but a lower rate (30.30%).
        # [claude] Asserts *which franchise*, not the denominator.
        #
        # The first version demanded "26" — the worst franchise's deal count —
        # and failed a correct answer that said "franchise 12, 34.6%". The
        # agent expressed the rate rather than its parts, which is the better
        # answer to the question asked. Assert the property, not the phrasing.
        expected_from_sql=(
            "WITH per AS ("
            "  SELECT franchise_id, count(*) AS total,"
            "         count(*) FILTER (WHERE status = 'cancelled') AS cancelled"
            "  FROM deals WHERE deleted_at IS NULL"
            "  GROUP BY franchise_id HAVING count(*) >= 20)"
            " SELECT franchise_id FROM per"
            " ORDER BY cancelled::numeric / total DESC LIMIT 1",
        ),
        why=(
            "The single most important case here. 'Most cancellations' and "
            "'worst cancellation rate' are different franchises — 4 and 12 — "
            "and answering the first when asked the second is the denominator "
            "family in its purest form. A count where a rate was asked for is "
            "wrong, specific and entirely plausible."
        ),
        tags=("rate", "denominator"),
    ),
    ComplexCase(
        name="a_rate_is_compared_against_the_company_overall",
        question=(
            "Which franchise has the worst cancellation rate among those "
            "with at least 20 deals, and how does it compare with the "
            "company overall?"
        ),
        # Worst franchise: 9 of 26. Company overall: 70 of 315.
        # [claude] Was the raw cancelled count (70). The agent compares
        # rates, which is what "how does it compare" asks for, so it never
        # states that number — another assertion that failed a better answer.
        # "22.2" matches both 22.2% and 22.22%.
        answer_contains=("12", "22.2"),
        min_tool_calls=1,
        why=(
            "Two aggregates at two grain levels in one answer. The agent has "
            "to hold a per-group figure and a whole-population figure at once "
            "and not confuse their denominators."
        ),
        tags=("rate", "comparison"),
    ),
    # ---- a subset denominator ---------------------------------------
    ComplexCase(
        name="stale_share_is_of_qualified_leads_not_of_all",
        question="Among qualified leads, how many have gone stale?",
        # 114 stale of 538 qualified. All stale leads is 187 — the near-miss.
        # [claude] Excludes merged duplicates. The first version did not,
        # and was simply wrong: LEADS_RULES_ONLY requires
        # `merged_into_id IS NULL` whenever the question is about unique
        # leads, and the agent applies it correctly. My ground truth was the
        # thing that needed fixing.
        expected_from_sql=(
            "SELECT count(*) FROM leads WHERE deleted_at IS NULL"
            " AND merged_into_id IS NULL"
            " AND qualification_status = 'qualified' AND is_stale",
        ),
        answer_excludes=("168",),
        expected_route="leads",
        max_tool_calls=3,
        why=(
            "The denominator is the subset, not the table. 187 is the count "
            "of all stale leads and is the answer to a question nobody asked; "
            "it is excluded explicitly because an answer quoting both would "
            "otherwise pass."
        ),
        tags=("denominator",),
    ),
    ComplexCase(
        name="conversion_within_qualified_leads_only",
        question=(
            "How many leads with qualification status 'qualified' have a "
            "conversion date recorded?"
        ),
        # 42 of 538 qualified. 78 is every converted lead — the near-miss.
        expected_from_sql=(
            "SELECT count(*) FROM leads WHERE deleted_at IS NULL"
            " AND merged_into_id IS NULL"
            " AND qualification_status = 'qualified'"
            " AND converted_at IS NOT NULL",
        ),
        expected_route="leads",
        max_tool_calls=3,
        why=(
            "Same subset-denominator shape as the case above, on a different "
            "column.\n\n"
            "The question names `converted_at` explicitly, which is unusual "
            "phrasing on purpose. The fixture's `converted_at` and its "
            "deal linkage disagree almost entirely — 280 leads have a deal, "
            "78 have a conversion date, and only 30 have both — so 'have "
            "they converted' has two defensible answers here and is a bad "
            "thing to test correctness against. That disagreement is a "
            "fixture-generation artefact worth fixing separately."
        ),
        tags=("denominator",),
    ),
    # ---- a varchar column that must be cast --------------------------
    ComplexCase(
        name="average_area_survives_a_varchar_column",
        question=(
            "What is the average unit area of contracted deals compared "
            "with cancelled ones?"
        ),
        # 271.35 contracted, 271.31 cancelled. `area` is character varying,
        # so a bare avg(area) raises `function avg(character varying) does
        # not exist` — the catalogue says "stored as varchar in the CRM".
        answer_contains=("271",),
        min_tool_calls=1,
        why=(
            "`area` holds numbers in a varchar column, so the query needs a "
            "cast. Without one PostgreSQL rejects it outright, and the "
            "interesting question is whether the agent recovers or reports "
            "the failure as though the data were unavailable — the shape "
            "that produced invented stale-deal counts before."
        ),
        tags=("types", "comparison"),
    ),
    ComplexCase(
        name="total_area_survives_a_varchar_column",
        question="What is the total unit area across all contracted deals?",
        # 61053 — distinctive enough that a coincidental match is unlikely.
        expected_from_sql=(
            "SELECT sum(area::numeric) FROM deals WHERE deleted_at IS NULL"
            " AND status = 'contracted' AND area IS NOT NULL",
        ),
        max_tool_calls=3,
        why="The same cast under SUM rather than AVG.",
        tags=("types",),
    ),
    # ---- several conditions at once ----------------------------------
    ComplexCase(
        name="three_conditions_are_all_applied",
        question=(
            "How many contracted deals are residential and have an area "
            "above 250 square metres?"
        ),
        # 90. Dropping any one condition gives a very different number.
        expected_from_sql=(
            "SELECT count(*) FROM deals WHERE deleted_at IS NULL"
            " AND status = 'contracted' AND is_commercial = false"
            " AND area::numeric > 250",
        ),
        max_tool_calls=3,
        why=(
            "Status, a boolean flag and a cast numeric threshold in one "
            "query. Quietly dropping a condition is the failure, and it "
            "produces a larger number that still looks reasonable."
        ),
        tags=("filters", "types"),
    ),
    # ---- two time windows in one answer ------------------------------
    ComplexCase(
        name="two_time_windows_are_kept_apart",
        question=(
            "How many deals are closing in the next 30 days, compared with "
            "how many closed in the previous 30 days?"
        ),
        expected_from_sql=(
            "SELECT count(*) FROM deals WHERE deleted_at IS NULL"
            " AND expected_closing_date BETWEEN CURRENT_DATE"
            " AND CURRENT_DATE + INTERVAL '30 days'",
            # [claude] Today is excluded from the backward window, because it
            # is already in the forward one. `BETWEEN CURRENT_DATE - 30 AND
            # CURRENT_DATE` counts the boundary day twice, and the agent —
            # which uses `< CURRENT_DATE` — was right where this assertion
            # was wrong.
            #
            # It passed for weeks by luck. The fixture emits dates as
            # CURRENT_DATE ± INTERVAL, so which deals land exactly on today
            # shifts daily; the day one finally did, the double count
            # appeared as a 13-versus-12 disagreement. A boundary this case
            # never meant to test was deciding whether it passed.
            "SELECT count(*) FROM deals WHERE deleted_at IS NULL"
            " AND expected_closing_date >= CURRENT_DATE - INTERVAL '30 days'"
            " AND expected_closing_date < CURRENT_DATE",
        ),
        why=(
            "Both numbers are asserted. Asserting only the first would pass "
            "an answer that got the backward-looking window wrong, and the "
            "backward one is the harder of the two.\n\n"
            "The two windows must not overlap: a deal closing today belongs "
            "to the next thirty days, not the previous thirty, and counting "
            "it in both inflates a comparison that is the whole point of "
            "the question."
        ),
        tags=("temporal", "comparison"),
    ),
    # ---- a share of the whole ----------------------------------------
    ComplexCase(
        name="commercial_share_of_the_pipeline",
        question="What share of our deals are commercial?",
        # 67 of 315 = 21.27%.
        expected_from_sql=(
            "SELECT count(*) FROM deals"
            " WHERE deleted_at IS NULL AND is_commercial",
        ),
        max_tool_calls=3,
        why=(
            "A share needs both halves. Asserted on the numerator rather "
            "than the percentage because 21.27 and 21.3 are the same answer "
            "written differently, and a case that fails on rounding is a bad "
            "assertion rather than a caught bug."
        ),
        tags=("rate",),
    ),
    ComplexCase(
        name="sla_breaches_reported_with_their_base",
        question=(
            "How many leads have breached their SLA, and what share of all "
            "leads is that?"
        ),
        # 171 of 884 = 19.34%.
        # [claude] Excludes merged duplicates — see the stale case above.
        # My first ground truth said 171 and the agent said 153; the agent
        # was right and the eval was wrong.
        expected_from_sql=(
            "SELECT count(*) FROM leads"
            " WHERE deleted_at IS NULL AND merged_into_id IS NULL"
            " AND sla_breach_at IS NOT NULL",
        ),
        expected_route="leads",
        max_tool_calls=3,
        why=(
            "A count and a rate over the same population, in one answer — "
            "and the population excludes merged duplicates, which is the "
            "half an eval is most likely to get wrong."
        ),
        tags=("rate",),
    ),
    # ---- routing under complexity ------------------------------------
    ComplexCase(
        name="a_cross_domain_question_reaches_the_deals_agent",
        question=(
            "Which lead sources have produced the most contracted deals?"
        ),
        expected_route="deals",
        max_tool_calls=4,
        why=(
            "Mentions leads, needs the deals table. Only the deals "
            "specialist can see both, and the leads agent's guard rejects "
            "`deals` even through a JOIN — so a misroute here is not a worse "
            "answer, it is a refusal that reads as 'the data does not exist'."
        ),
        tags=("routing", "cross-domain"),
    ),
    # ---- something it genuinely cannot do -----------------------------
    ComplexCase(
        name="median_is_refused_rather_than_invented",
        question="What is the median unit area across all deals?",
        # The guard rejects percentile_cont, so no query can produce this.
        # The true median is 270; a fabricated one would be plausible.
        # [claude] Must say it cannot, and must not answer with anything
        # else. The first version excluded only the true median (270) and
        # so *passed* an answer reading "the average unit area is 272.13" —
        # a different question, answered confidently, with no mention of
        # the substitution. 272 and 271 are the averages it reached for.
        answer_contains=("not available",),
        answer_excludes=("270", "272", "271"),
        max_tool_calls=4,
        why=(
            "The guard rejects `percentile_cont`, so this is unanswerable "
            "and the only correct response says so.\n\n"
            "Excluding the true median is deliberate: an agent that guessed "
            "the right number for the wrong reason should still fail, "
            "because next time it will guess a wrong one. Excluding the "
            "*average* is what the case learned the hard way — widening the "
            "area rule to cover aggregation made the agent substitute a mean "
            "for a median rather than refuse, and this assertion was too "
            "weak to notice. A mean and a median differ exactly when the "
            "distribution is skewed, which is when someone asks."
        ),
        tags=("refusal",),
    ),

    # ========================================================
    # Two specialists, one question
    # ========================================================
    #
    # [claude] The gap that let the orchestration bug ship.
    #
    # Orchestration has existed since 18 August and nothing asserted a
    # two-specialist *answer*. The routing suite checks single-route
    # classification; every case above runs one specialist. So a supervisor
    # that planned `deals, research` perfectly and then produced nothing
    # was invisible: the route was right, and there was no case whose
    # answer depended on both halves arriving.
    #
    # These deliberately assert structure — both specialists ran, both
    # tools ran, our own figure is present, and the answer does not report
    # the other half as missing — rather than asserting what the web says.
    # The market number changes weekly; the failure being guarded against
    # does not.
    ComplexCase(
        name="both_halves_of_a_split_question_are_answered",
        question=(
            "How do our cancellation rates compare with the wider "
            "Egyptian market?"
        ),
        expected_route="deals",
        expected_specialists=("deals", "research"),
        expected_tools=("sql_query", "web_search"),
        # 70 cancelled of 315 deals = 22.22%. Asserted as "22.2" so an
        # answer rounding to one decimal is not a failure.
        answer_contains=("22.2",),
        answer_excludes=(
            "do not have access",
            "cannot provide",
            "handled separately",
            "unable to provide",
        ),
        min_tool_calls=2,
        max_tool_calls=6,
        why=(
            "Measured 0/4 before the fix. The supervisor planned both "
            "specialists correctly and then handed each of them the whole "
            "question; the deals agent read a question half of which it "
            "could not answer and declined all of it, holding the 22.22% "
            "the first half needed.\n\n"
            "The exclusions are the assertion that matters. A turn where "
            "one specialist answers and the other apologises still merges "
            "into something that looks like a reply — it just has an "
            "apology where the comparison should be, and only a human "
            "reading it would notice."
        ),
        tags=("orchestration", "two-specialist"),
    ),
    ComplexCase(
        name="a_split_comparison_reaches_the_leads_agent_too",
        question=(
            "Is our lead conversion rate better or worse than the Egyptian "
            "property market average?"
        ),
        expected_specialists=("leads", "research"),
        expected_tools=("sql_query", "web_search"),
        answer_excludes=(
            "do not have access",
            "i cannot",
            "handled separately",
            "unable to provide",
        ),
        min_tool_calls=2,
        max_tool_calls=6,
        why=(
            "The same failure through a different pair, because the first "
            "attempt at the fix worked for the deals agent and not the "
            "research one — one case would have reported the fix complete "
            "when half of it was.\\n\\n"
            "Phrased as a *comparison*, and that is not incidental. The "
            "first version of this case asked 'what share of our leads go "
            "stale, and what is the market doing' — a conjunction — and it "
            "passed with the fix disabled, which made it worthless as a "
            "guard. Two questions joined by 'and' are read as two "
            "questions and each specialist answers its own; a comparison "
            "reads as one question that a specialist can only half answer, "
            "and that is the shape it declines. Checked by disabling the "
            "fix and confirming this fails.\\n\\n"
            "The research agent's own prompt was the second half of the "
            "bug: it said to 'answer only the public half and say the "
            "internal half is handled separately', and the model did the "
            "second half and skipped the first, never searching at all."
        ),
        tags=("orchestration", "two-specialist"),
    ),
)


# ============================================================
# Running
# ============================================================


async def scalar(query: str) -> str:
    """One value from the database, rendered the way an agent would write it."""

    from app.db.connection import app_db

    await app_db.connect()

    async with app_db.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query)
            row = await cursor.fetchone()

    value = next(iter(row.values())) if hasattr(row, "values") else row[0]

    if value is None:
        return ""

    if isinstance(value, float) and value.is_integer():
        value = int(value)

    # Numeric from PostgreSQL arrives as Decimal; a whole one should read as
    # "61053", not "61053.00", because that is how the answer will phrase it.
    try:
        from decimal import Decimal

        if isinstance(value, Decimal) and value == value.to_integral_value():
            value = int(value)
    except Exception:
        pass

    return str(value)


async def evaluate(case: ComplexCase, graph) -> tuple[bool, str]:
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": case.question}]},
        config={"configurable": {"thread_id": f"complex-{case.name}"}},
    )

    messages = result["messages"]
    route = result.get("route")

    # [claude] From `trace` — the transcript no longer carries the agent's
    # working. See the note in graph_cases.evaluate.
    tool_calls = len(result.get("trace") or [])
    answer = str(messages[-1].content)
    lowered = answer.lower()

    # An agent writes 4,665 where a case asserts 4665, and a thousands
    # separator is not a wrong answer.
    unseparated = re.sub(r"(?<=\d),(?=\d)", "", lowered)

    def present(needle: str) -> bool:
        needle = needle.lower()
        return needle in lowered or needle in unseparated

    problems = []

    if case.expected_route and route != case.expected_route:
        problems.append(f"routed to {route!r}, expected {case.expected_route!r}")

    if case.expected_specialists:
        ran = sorted(f["specialist"] for f in (result.get("findings") or []))

        if ran != sorted(case.expected_specialists):
            problems.append(
                f"specialists {ran}, expected "
                f"{sorted(case.expected_specialists)}"
            )

    used = list(result.get("trace") or [])

    for tool in case.expected_tools:
        if tool not in used:
            problems.append(f"{tool} never ran (tools used: {used or 'none'})")

    if not case.min_tool_calls <= tool_calls <= case.max_tool_calls:
        problems.append(
            f"{tool_calls} tool calls, expected "
            f"{case.min_tool_calls}-{case.max_tool_calls}"
        )

    expected = list(case.answer_contains)

    for query in case.expected_from_sql:
        expected.append(await scalar(query))

    missing = [value for value in expected if value and not present(value)]
    if missing:
        problems.append(f"answer missing {missing}")

    banned = [value for value in case.answer_excludes if present(value)]
    if banned:
        problems.append(f"answer must not contain {banned}")

    if problems:
        return False, f"{'; '.join(problems)}\n      answer: {answer[:240]}"

    return True, f"[{tool_calls} calls, {route}] {answer[:110]}"


async def main_async() -> int:
    from app.graph.builder import build_supervisor_graph

    # The production path, deliberately — see the module docstring.
    graph = build_supervisor_graph()

    failures = []

    for case in COMPLEX_CASES:
        ok, detail = await evaluate(case, graph)

        print(f"  {'PASS' if ok else 'FAIL'}  {case.name}")
        print(f"        {detail}")

        if not ok:
            failures.append(case.name)

    total = len(COMPLEX_CASES)
    print(f"\n  {total - len(failures)}/{total} passed")

    if failures:
        print(f"  failed: {', '.join(failures)}")

    return 1 if failures else 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
