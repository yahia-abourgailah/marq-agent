"""
[claude] The supervisor — routes a question to the domain agent that owns it.

Why a router and not handoff tools
----------------------------------
The domain agents are deliberately sealed: each one's SQLGuard permits only
the tables its prompt describes, so the Leads Agent cannot query `deals` even
if it wanted to. Letting agents hand off to each other would mean every agent
carries a transfer tool and decides mid-answer to abandon its turn, which is
harder to reason about and harder to test than one routing decision taken up
front.

So routing is a single classification call before any agent runs. It costs
one extra model round-trip per turn and produces a decision that is visible
in the trace and assertable in an eval.

How the boundary falls
----------------------
The two domains are not symmetric, and that decides most routes:

    DEALS      sees deals, leads and users
    LEADS      sees leads and users
    WORKSPACE  sees deals, leads and users — plus the user's uploaded files

So a question spanning both — "which lead sources produce the most contracted
deals" — goes to DEALS, which is the only agent that can join them. LEADS
gets the questions that live entirely in the funnel. This is why there is no
"cannot answer, it spans two domains" outcome: the overlap is already covered.

[claude] WORKSPACE extends the same reasoning one level up. It holds the CRM
superset *and* the uploaded files, so it is the only agent that can answer
"does my spreadsheet agree with the CRM". That makes it the first test in the
routing order rather than the last: a reconciliation question mentions deals
too, and whichever domain is checked first would otherwise claim it and
answer half of it convincingly.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage

# Routes the supervisor may choose. OUT_OF_SCOPE short-circuits to a direct
# reply rather than sending a non-CRM question to an agent that would waste a
# database round-trip discovering it cannot answer.
DEALS_ROUTE = "deals"
LEADS_ROUTE = "leads"
WORKSPACE_ROUTE = "workspace"
# [claude] Was `out_of_scope`, which returned one canned sentence. It is a
# real conversational agent now — greetings, "what can you do", general
# questions — so the name says that. A route called out_of_scope that
# answers helpfully is a name that lies, and this codebase keeps getting
# bitten by those.
RESEARCH_ROUTE = "research"
GENERAL_ROUTE = "general"

# Kept as an alias so nothing importing the old name breaks silently.
OUT_OF_SCOPE = GENERAL_ROUTE

# [claude] WORKSPACE_ROUTE is listed before the CRM domains because
# parse_route() matches in order and the workspace agent is the only one that
# can see both an uploaded file and the CRM. A question naming both must not
# be claimed by whichever route happens to be checked first.
VALID_ROUTES = (
    WORKSPACE_ROUTE,
    DEALS_ROUTE,
    LEADS_ROUTE,
    RESEARCH_ROUTE,
    GENERAL_ROUTE,
)

# DEALS is the superset domain, so it is the safe landing place when the
# classifier returns something unparseable.
FALLBACK_ROUTE = DEALS_ROUTE


SUPERVISOR_PROMPT = """\
You route a CRM question to the specialist that owns it. You never answer the
question yourself.

Reply with exactly one word and nothing else:

  deals          The question is about deals — pipeline, status, closings,
                 contracts, reservations, cancellations, units, areas,
                 clients, deal owners, approvals, collections, and the
                 franchises, projects, developers and locations a deal
                 belongs to.

                 Also choose deals for any question that needs BOTH deals and
                 leads at once, such as "which lead sources produce the most
                 contracted deals". Only the deals specialist can see both.

  leads          The question is entirely about leads — where demand comes
                 from, lead stages and the funnel, staleness, response times
                 and SLA, qualification and scoring, duplicates, campaigns
                 and utm sources, lead conversion.

  workspace      The question refers to a file the user uploaded — a
                 spreadsheet, an Excel or CSV file, a PDF, a document, a
                 report, "my file", "my sheet", "the attachment", "the data
                 I sent", "this contract".

                 Also choose workspace for anything comparing, reconciling,
                 checking or matching an uploaded file against the CRM.
                 Only the workspace specialist can see both.

  research       The question needs information from outside the company —
                 market conditions, news, regulations, a developer's public
                 reputation, prices or trends in the wider market, general
                 facts. Anything the CRM cannot know because it is not
                 about MarQ's own records.

                 NEVER choose research for MarQ's own deals, leads, clients
                 or performance. That data is private and is not sent
                 outside the company.

  general        The turn has no CRM subject at all — a greeting ("hi",
                 "good morning", "thanks"), a question about what you can
                 do, general knowledge, chit-chat, or a writing task.

                 Vague business questions are NOT general. "How is our
                 business doing", "give me an overview", "how did we perform
                 this quarter" are asking about the CRM in general terms;
                 send them to deals, which holds the pipeline.

Deciding — work down this list and stop at the first match:

1. Does the question refer to an uploaded file at all — a sheet, a
   spreadsheet, a document, a PDF, an attachment, "my data"?  -> workspace

   This wins even when the question is mostly about deals or leads.
   "Does my sheet match our contracted deals", "check this list against the
   CRM", "how do the numbers in my file compare" all need the file and the
   CRM together, and only the workspace specialist has both. Sending them to
   deals gets an answer about the CRM alone, which looks right and silently
   ignores half the question.

2. Does the question involve deals at all — deals, contracts, closings,
   reservations, pipeline — even alongside leads?  -> deals

   This wins even when the thing being counted is a lead. "How many leads
   became deals", "how many leads could become deals", "which lead sources
   produce deals" all need the deals table, and only the deals specialist
   has it. Sending them to leads gets a refusal from an agent that cannot
   see deals, which reads to the user as though the data does not exist.

3. Is it entirely about leads, with deals never mentioned or implied?
   -> leads

4. Does it ask you to do something WITH the previous answer rather than
   ask something new — "chart that", "give me a graph", "show it as a
   table", "break that down", "why?", "and last year?"  ->  stay with the
   specialist that answered last.

   This outranks rule 5. "Give me a graph" has no CRM subject in it, so
   rule 5 would send it to `general` — which does not have the figures the
   previous turn just produced, and would either refuse or invent. A
   follow-up belongs to whoever holds the data it refers to.

5. Is there no CRM subject at all, and no reference to a previous answer?
   -> general

6. Does it mention none of them by name and continue the previous turn?
   -> stay with the specialist that answered last

Rules 1 and 2 outrank rules 4 and 6: if a follow-up brings a file into a
deals conversation it moves to workspace, and if it brings deals into a
leads conversation it moves to deals.

Restricted or unavailable data is NOT general. If the subject is a CRM
thing — a deal, a lead, a franchise, a project, a stage, a source, a user —
route it to a specialist even when you suspect the answer is unavailable.
The specialist explains what it cannot provide; that is its job, not yours.

  "What is the total contract price"  -> deals (masked, the agent says so)
  "What is the name of franchise 9"   -> deals (no lookup table, the agent
                                        says so)

Reserve general for turns with no CRM subject at all.

An uploaded file the user believes exists is never general either. If
they refer to a file, route to workspace; that specialist reports when there
is nothing uploaded.

COMBINING SPECIALISTS
---------------------
Most turns need exactly one specialist. Some genuinely need two, and
answering only half of one of those looks like a complete answer, which is
worse than saying you cannot.

Reply with more than one name, separated by a comma, ONLY when the question
plainly has two halves that different specialists own:

  "How do our cancellation rates compare with the market?"
      -> deals, research
      (ours is in the CRM; the market is not)

  "Summarise our pipeline and any news about the new capital"
      -> deals, research

  "Does my sheet match the CRM, and is that developer reputable?"
      -> workspace, research

Do NOT combine when one specialist already covers the question:

  "Which lead sources produce the most contracted deals"  -> deals
      (deals sees leads too — one specialist, not two)
  "How many deals are contracted"                         -> deals
  "hi"                                                    -> general

Never combine `general` with anything: it is for turns with no subject.

Reply with one word — deals, leads, workspace, research, or general — or
two names separated by a comma when the question truly has two halves."""


# [claude] No longer the general reply — the General Agent writes its own.
# Kept as the degraded answer for when that model call fails, because a
# fixed sentence is a better outcome than a stack trace.
OUT_OF_SCOPE_REPLY = (
    "I can help with MarQ CRM data — deals and leads — and with files you "
    "upload. Ask me about pipeline, lead sources, stages or response times, "
    "or point me at a spreadsheet or document, and I'll take a look."
)


# [claude] Punctuation a one-word reply arrives wrapped in: "deals.",
# "**workspace**", "leads,".
_TRIM = ".,:;!?\"'`*_ \t\n"

# A route name the classifier explicitly ruled out. Matched with a small
# window so "not a deals question" and "no workspace file" both count, while
# an unrelated "not" earlier in the sentence does not reach across.
_NEGATED = re.compile(
    r"\b(?:not|no|never|isn'?t|aren'?t|rather\s+than|instead\s+of)\b"
    r"[^.;!?]{0,20}?\b(" + "|".join(VALID_ROUTES) + r")\b"
)


def parse_route(text: str) -> str:
    """
    Map raw classifier output onto a valid route.

    Kept forgiving on purpose: a small model asked for one word sometimes
    returns `deals.` or `Route: deals`. Anything unrecognised falls back to
    DEALS, which is the superset domain, rather than failing the turn.
    """

    lowered = str(text).strip().lower()

    # [claude] The route was renamed from `out_of_scope` to `general`, and a
    # classifier trained on nothing in particular still reaches for the old
    # word — it is the more natural phrase for the category. Accepting it
    # costs nothing and stops an off-topic turn falling through to the deals
    # fallback, which is a database round-trip to discover what a greeting
    # already made obvious.
    lowered = lowered.replace("out_of_scope", GENERAL_ROUTE)
    lowered = lowered.replace("out of scope", GENERAL_ROUTE)

    # [claude] 1. The token on its own, punctuation trimmed.
    #
    # This is the path the classifier takes almost every time, and the only
    # one the 37 routing cases exercised. Everything below exists for the
    # times it does not — which the handoff notes are exactly the times
    # vLLM's batching makes decoding non-reproducible.
    token = lowered.strip(_TRIM)

    if token in VALID_ROUTES:
        return token

    # 2. Drop negated mentions before matching anything.
    #
    # Substring matching in tuple order used to resolve any prose reply to
    # whichever route name came first in VALID_ROUTES, so "Not a deals
    # question — route to leads" returned deals. Position cannot fix it:
    # that reply wants the *last* mention while "This is about leads, not
    # deals." wants the *first*. What actually distinguishes them is the
    # negation, so the negated span is removed and whatever survives is the
    # classifier's real answer.
    remaining = _NEGATED.sub(" ", lowered)

    # 3. `general` wins over anything still standing.
    #
    # It is the one route name that cannot appear incidentally inside
    # another, and misrouting a greeting into a full CRM agent is the more
    # expensive mistake — it costs a database round-trip to discover what
    # "hi" already made obvious.
    if GENERAL_ROUTE in remaining:
        return GENERAL_ROUTE

    # 4. The earliest surviving mention.
    positions = [
        (remaining.index(route), route)
        for route in VALID_ROUTES
        if route in remaining
    ]

    if positions:
        return min(positions)[1]

    # 5. Nothing recognisable. DEALS is the superset domain, so it is the
    # least-wrong landing place.
    return FALLBACK_ROUTE


def _conversation(messages: list[AnyMessage], limit: int = 6) -> list[dict]:
    """
    Recent turns, so a follow-up like "and how many of those closed?" routes
    to whoever answered last.

    Tool calls and tool results are dropped — they are an implementation
    detail of the previous turn and would swamp the classifier.

    [claude] Filtered first, then sliced. It used to be the other way round,
    and the order was the bug.

    `messages[-limit:]` took the last six *raw* messages and only then
    discarded tool traffic. A turn that made several tool calls filled that
    window with tool-call `AIMessage`s and their `ToolMessage` results, all
    of which are dropped — so the classifier was left with the current
    question and nothing else. Workspace turns run to 28 steps, which makes
    the reconciliation turns precisely the ones after which a follow-up lost
    its history; and a bare follow-up with no history falls through
    `parse_plan` to the `deals` fallback.

    So `limit` now means six *exchanges*, which is what the parameter has
    always read as. The transcript no longer carries tool traffic either —
    see state.py — but this does not rely on that: a checkpoint written
    before that change still replays through here correctly.
    """

    kept = []

    for message in messages:
        role = getattr(message, "type", None)

        if role == "human":
            kept.append({"role": "user", "content": str(message.content)})
        elif role == "ai" and str(message.content).strip():
            kept.append({"role": "assistant", "content": str(message.content)})

    return kept[-limit:]


def parse_plan(text: str) -> list[str]:
    """
    Map classifier output onto one or more routes.

    [claude] A reply is only a plan when **every** comma-separated part is a
    bare route name. Anything else goes to `parse_route` whole.

    The first version simply split on commas, and prose full of commas is
    the common case rather than the exotic one: "This is about leads, not
    deals." became ["leads", "deals"] — fanning out to two specialists on a
    *negation*, and answering with the very domain the classifier had just
    rejected. That is the same shape as the order-dependent substring bug
    `parse_route` was rewritten to fix, reintroduced one layer up.

    Requiring every part to be a bare token is what separates "deals,
    research" from prose, and it fails safe: an unrecognised reply keeps the
    single-route behaviour, including the negation handling and the fallback
    to deals.
    """

    raw = str(text).strip()

    if "," not in raw:
        return [parse_route(raw)]

    # [claude] A leading label is common and harmless — "Route: deals,
    # research" is a plan wearing a hat. Stripped before splitting so the
    # strictness below does not reject it as prose.
    lowered = raw.lower()

    for label in ("route:", "routes:", "plan:", "answer:", "specialists:"):
        if lowered.startswith(label):
            lowered = lowered[len(label) :]
            break

    parts = [part.strip().strip(_TRIM) for part in lowered.split(",")]
    parts = [part for part in parts if part]

    # Prose, not a plan — hand the whole reply to the single-route reader.
    if not parts or not all(part in VALID_ROUTES for part in parts):
        return [parse_route(raw)]

    seen: list[str] = []

    for part in parts:
        if part not in seen:
            seen.append(part)

    # `general` is for turns with no subject, so it cannot sensibly be
    # combined; if a specialist was also named, that is the real answer.
    if len(seen) > 1 and GENERAL_ROUTE in seen:
        seen = [route for route in seen if route != GENERAL_ROUTE]

    # Two specialists is the most a question has genuinely needed, and each
    # one costs a full agent run. Capped rather than trusted.
    return seen[:2]


async def choose_route(model: Any, messages: list[AnyMessage]) -> str:
    """Classify the current turn into one of VALID_ROUTES."""

    if not messages:
        return FALLBACK_ROUTE

    response = await model.ainvoke(
        [
            {"role": "system", "content": SUPERVISOR_PROMPT},
            *_conversation(messages),
        ]
    )

    return parse_route(response.content)


async def choose_plan(model: Any, messages: list[AnyMessage]) -> list[str]:
    """
    Classify the turn into one or more routes.

    [claude] The same call `choose_route` makes — same prompt, same message
    trimming — read with `parse_plan` instead of `parse_route`, so a
    comma-separated reply becomes a plan and everything else behaves
    exactly as it did.
    """

    if not messages:
        return [FALLBACK_ROUTE]

    response = await model.ainvoke(
        [
            {"role": "system", "content": SUPERVISOR_PROMPT},
            *_conversation(messages),
        ]
    )

    return parse_plan(response.content)


def out_of_scope_message() -> AIMessage:
    return AIMessage(content=OUT_OF_SCOPE_REPLY)


__all__ = [
    "DEALS_ROUTE",
    "GENERAL_ROUTE",  # [claude]
    "FALLBACK_ROUTE",
    "LEADS_ROUTE",
    "OUT_OF_SCOPE",
    "OUT_OF_SCOPE_REPLY",
    "SUPERVISOR_PROMPT",
    "VALID_ROUTES",
    "WORKSPACE_ROUTE",  # [claude]
    "RESEARCH_ROUTE",  # [claude]
    "choose_route",
    "choose_plan",  # [claude]
    "parse_plan",  # [claude]
    "out_of_scope_message",
    "parse_route",
]
