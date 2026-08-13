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

    DEALS  sees deals, leads and users
    LEADS  sees leads and users

So a question spanning both — "which lead sources produce the most contracted
deals" — goes to DEALS, which is the only agent that can join them. LEADS
gets the questions that live entirely in the funnel. This is why there is no
"cannot answer, it spans two domains" outcome: the overlap is already covered.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AnyMessage

# Routes the supervisor may choose. OUT_OF_SCOPE short-circuits to a direct
# reply rather than sending a non-CRM question to an agent that would waste a
# database round-trip discovering it cannot answer.
DEALS_ROUTE = "deals"
LEADS_ROUTE = "leads"
OUT_OF_SCOPE = "out_of_scope"

VALID_ROUTES = (DEALS_ROUTE, LEADS_ROUTE, OUT_OF_SCOPE)

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

  out_of_scope   The question has nothing to do with the CRM at all — the
                 weather, general knowledge, chit-chat, writing tasks.

                 Vague business questions are NOT out of scope. "How is our
                 business doing", "give me an overview", "how did we perform
                 this quarter" are asking about the CRM in general terms;
                 send them to deals, which holds the pipeline.

Deciding — work down this list and stop at the first match:

1. Does the question involve deals at all — deals, contracts, closings,
   reservations, pipeline — even alongside leads?  -> deals

   This wins even when the thing being counted is a lead. "How many leads
   became deals", "how many leads could become deals", "which lead sources
   produce deals" all need the deals table, and only the deals specialist
   has it. Sending them to leads gets a refusal from an agent that cannot
   see deals, which reads to the user as though the data does not exist.

2. Is it entirely about leads, with deals never mentioned or implied?
   -> leads

3. Is there no CRM subject at all?  -> out_of_scope

4. Does it mention neither by name and continue the previous turn?
   -> stay with the specialist that answered last

Rule 1 outranks rule 4: if a follow-up brings deals into a leads
conversation, it moves to deals.

Restricted or unavailable data is NOT out_of_scope. If the subject is a CRM
thing — a deal, a lead, a franchise, a project, a stage, a source, a user —
route it to a specialist even when you suspect the answer is unavailable.
The specialist explains what it cannot provide; that is its job, not yours.

  "What is the total contract price"  -> deals (masked, the agent says so)
  "What is the name of franchise 9"   -> deals (no lookup table, the agent
                                        says so)

Reserve out_of_scope for questions with no CRM subject at all.

Reply with one word: deals, leads, or out_of_scope."""


OUT_OF_SCOPE_REPLY = (
    "I can only help with MarQ CRM data — deals and leads. Ask me about "
    "pipeline, lead sources, stages or response times and I'll take a look."
)


def parse_route(text: str) -> str:
    """
    Map raw classifier output onto a valid route.

    Kept forgiving on purpose: a small model asked for one word sometimes
    returns `deals.` or `Route: deals`. Anything unrecognised falls back to
    DEALS, which is the superset domain, rather than failing the turn.
    """

    lowered = str(text).strip().lower()

    for route in VALID_ROUTES:
        if route in lowered:
            return route

    return FALLBACK_ROUTE


def _conversation(messages: list[AnyMessage], limit: int = 6) -> list[dict]:
    """
    Recent turns, so a follow-up like "and how many of those closed?" routes
    to whoever answered last.

    Tool calls and tool results are dropped — they are an implementation
    detail of the previous turn and would swamp the classifier.
    """

    kept = []

    for message in messages[-limit:]:
        role = getattr(message, "type", None)

        if role == "human":
            kept.append({"role": "user", "content": str(message.content)})
        elif role == "ai" and str(message.content).strip():
            kept.append({"role": "assistant", "content": str(message.content)})

    return kept


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


def out_of_scope_message() -> AIMessage:
    return AIMessage(content=OUT_OF_SCOPE_REPLY)


__all__ = [
    "DEALS_ROUTE",
    "FALLBACK_ROUTE",
    "LEADS_ROUTE",
    "OUT_OF_SCOPE",
    "OUT_OF_SCOPE_REPLY",
    "SUPERVISOR_PROMPT",
    "VALID_ROUTES",
    "choose_route",
    "out_of_scope_message",
    "parse_route",
]
