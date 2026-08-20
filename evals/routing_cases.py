"""
[claude] Routing cases for the supervisor.

`tests/test_supervisor.py` proves the parsing and the wiring. These prove the
classifier sends real questions to the right specialist — the thing that
actually breaks when the supervisor prompt is edited.

A misroute is not a crash: the Leads Agent asked a deals question declines
politely, so the failure is quiet. That is exactly why it needs an eval.

    python -m evals.routing_cases
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass(frozen=True)
class RouteCase:
    question: str
    expected: str
    why: str = ""
    # Prior turns, as (role, text), for follow-up routing.
    history: tuple[tuple[str, str], ...] = ()


ROUTE_CASES: tuple[RouteCase, ...] = (
    # ---- plainly deals ----------------------------------------------
    RouteCase("How many active deals do we have?", "deals"),
    RouteCase("Show me the 5 deals closing soonest.", "deals"),
    RouteCase("Which deals were cancelled last month?", "deals"),
    RouteCase("How many deals does Sara Mostafa own?", "deals"),
    RouteCase("What is the average unit area across our deals?", "deals"),
    # ---- plainly leads ----------------------------------------------
    RouteCase("How many unique leads do we have?", "leads"),
    RouteCase("How many leads are stale?", "leads"),
    RouteCase("What share of leads comes from each utm source?", "leads"),
    RouteCase("What is our average lead response time?", "leads"),
    RouteCase("How many leads are in each stage?", "leads"),
    # ---- spans both -> deals, the only domain that sees both ---------
    RouteCase(
        "Which lead sources produce the most contracted deals?",
        "deals",
        why="Needs leads and deals joined; only the deals domain can.",
    ),
    RouteCase(
        "How many of our leads turned into contracted deals?",
        "deals",
        why="The thing being counted is deals.",
    ),
    RouteCase(
        "How many leads could potentially become deals?",
        "deals",
        why=(
            "The thing counted is a lead, but answering needs the deals "
            "table. Routing on the counted noun sent this to leads, which "
            "cannot see deals and refused — reading to the user as though "
            "the data does not exist."
        ),
    ),
    RouteCase(
        "What sources have the most probability to become deals?",
        "deals",
        why="Mentions deals, so it outranks the leads-sounding subject.",
        history=(
            ("user", "classify leads by source"),
            ("assistant", "facebook 310, tiktok 160, snapchat 82"),
        ),
    ),
    # ---- restricted data is still in scope ---------------------------
    RouteCase(
        "What is the total contract price?",
        "deals",
        why=(
            "Masked data is a deals question the specialist declines — not "
            "an out-of-scope question."
        ),
    ),
    RouteCase(
        "What is the name of franchise 9?",
        "deals",
        why=(
            "A franchise is a CRM subject. The lookup table is unavailable, "
            "which the deals specialist explains — it is not general."
        ),
    ),
    RouteCase(
        "Which project has the most deals?",
        "deals",
        why="Projects hang off deals.",
    ),
    RouteCase(
        "How is our business doing?",
        "deals",
        why=(
            "Vague but genuinely about the CRM. Sending it to general "
            "tells the user their own pipeline is off-topic."
        ),
    ),
    RouteCase(
        "Give me an overview of how we performed this quarter.",
        "deals",
        why="A performance overview is a pipeline question.",
    ),
    # ---- uploaded files -> workspace ---------------------------------
    #
    # [claude] The workspace agent is the only one that can see an uploaded
    # file, so these misroute silently in the worst way: the deals agent
    # answers the CRM half of a reconciliation question convincingly and
    # never mentions that it could not open the file.
    RouteCase("What's in the spreadsheet I uploaded?", "workspace"),
    RouteCase("Summarise the PDF I just sent.", "workspace"),
    RouteCase("What are the payment terms in that contract?", "workspace"),
    RouteCase("How many rows are in my sheet?", "workspace"),
    RouteCase("What columns does my file have?", "workspace"),
    # ---- file + CRM together -> workspace, not deals ------------------
    RouteCase(
        "Does my spreadsheet match our contracted deals?",
        "workspace",
        why=(
            "Mentions deals, so the deals rule would claim it. Only the "
            "workspace agent holds both sides of a reconciliation."
        ),
    ),
    RouteCase(
        "Check this list of deals against the CRM.",
        "workspace",
        why="A comparison, even though 'deals' is the louder noun.",
    ),
    RouteCase(
        "Reconcile the amounts in my file with what we have recorded.",
        "workspace",
    ),
    RouteCase(
        "Are any of the leads in my upload missing from the system?",
        "workspace",
        why="Leads plus a file is still a workspace question.",
    ),
    RouteCase(
        "Compare the totals in my sheet to our pipeline.",
        "workspace",
    ),
    # ---- a file question with no file is still workspace --------------
    RouteCase(
        "What does my uploaded file say about penalties?",
        "workspace",
        why=(
            "Whether anything is actually uploaded is the specialist's to "
            "report. Routing it elsewhere answers a different question."
        ),
    ),
    # ---- no file mentioned -> unchanged -------------------------------
    RouteCase(
        "How many contracted deals do we have?",
        "deals",
        why="No file in sight; the workspace rule must not over-trigger.",
    ),
    RouteCase(
        "What share of leads comes from meta?",
        "leads",
        why="Guards against the workspace category swallowing plain CRM work.",
    ),
    # ---- follow-up brings a file in -> moves to workspace --------------
    RouteCase(
        "How does that compare with the sheet I uploaded?",
        "workspace",
        why="Rule 1 outranks staying with the previous specialist.",
        history=(
            ("user", "How many contracted deals do we have?"),
            ("assistant", "There are 225 contracted deals."),
        ),
    ),
    # ---- genuinely out of scope --------------------------------------
    # [claude] Was `general`, back when general meant "cannot help". The
    # weather is a fact about the outside world and the Research Agent can
    # now look it up, so `research` is the better answer — the eval was
    # stale, not the router.
    RouteCase("What is the weather in Cairo today?", "research"),
    # [claude] The two turns people actually open a chat window with. Both
    # used to reach a canned refusal, which reads as broken rather than
    # scoped — the reason `out_of_scope` became a real agent.
    RouteCase("hi", "general"),
    RouteCase("What can you help me with?", "general"),
    # Needs no facts, so it must NOT burn a web search.
    RouteCase("Write me a poem about real estate.", "general"),

    # ---- the research boundary -------------------------------------
    RouteCase("What is the outlook for the Egyptian property market?", "research"),
    RouteCase("Is the new administrative capital still growing?", "research"),
    # MarQ's own numbers are never a research question — that data is
    # private and must not reach a search provider.
    RouteCase("How is our own pipeline performing this quarter?", "deals"),
    # ---- follow-ups keep the thread ----------------------------------
    RouteCase(
        "And how many of those are commercial?",
        "deals",
        why="Follow-up to a deals answer with no subject of its own.",
        history=(
            ("user", "How many active deals do we have?"),
            ("assistant", "There are 245 active deals."),
        ),
    ),
    RouteCase(
        "What about the stale ones?",
        "leads",
        why="Follow-up to a leads answer.",
        history=(
            ("user", "How many unique leads do we have?"),
            ("assistant", "There are 786 unique leads."),
        ),
    ),
)


async def evaluate(case: RouteCase, model) -> tuple[bool, str]:
    from langchain_core.messages import AIMessage, HumanMessage

    from app.graph.supervisor import choose_route

    messages = [
        HumanMessage(content=text) if role == "user" else AIMessage(content=text)
        for role, text in case.history
    ]
    messages.append(HumanMessage(content=case.question))

    got = await choose_route(model, messages)
    return got == case.expected, got


async def main_async() -> int:
    from app.llm.model import get_model

    model = get_model()
    failures = []

    for case in ROUTE_CASES:
        ok, got = await evaluate(case, model)
        prefix = "PASS" if ok else "FAIL"
        print(f"  {prefix}  -> {got:<13} {case.question[:56]}")
        if not ok:
            failures.append(case.question)
            print(f"        expected {case.expected}"
                  + (f" — {case.why}" if case.why else ""))

    print(f"\n  {len(ROUTE_CASES) - len(failures)}/{len(ROUTE_CASES)} routed correctly")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
