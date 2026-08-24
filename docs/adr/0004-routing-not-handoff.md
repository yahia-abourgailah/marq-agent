# 0004 — One routing decision up front, not handoff tools

**Status:** Accepted · **Date:** 2026-08-24 (decided 2026-08-13, extended 2026-08-18)

## Context

Several specialists, one question. Either something decides who answers
before anyone runs, or the agents decide among themselves while running.

## Decision

A single classification call before any agent runs
(`app/graph/supervisor.py`). It returns one route, or two when the question
genuinely has two halves — capped at two. Specialists then run in parallel
and a synthesis step merges them.

## Alternatives

**Handoff tools.** Every agent carries a transfer tool and may abandon its
turn mid-answer. Harder to reason about, harder to test, and it puts a
capability in every agent's hands whose whole purpose is to cross the
boundary [0002](0002-domain-binds-tables-guard-and-prompt.md) exists to
draw.

**Always run every specialist and merge.** No misroutes, and it multiplies
cost by the number of domains while sending every question to agents that
cannot answer it.

## Consequences

- The decision is visible in the trace and assertable in an eval. 42 routing
  cases exist because a misroute is otherwise silent.
- Orchestration decides *who runs*, never *what they can reach*. A misroute
  cannot be rescued by an agent reaching into another domain's data, because
  there is no path for it to.
- A single specialist's answer passes through verbatim rather than being
  rewritten. Paraphrasing a figure verified against SQL is how a correct
  answer becomes wrong.
- Two specialists sharing a turn need telling so. Each receives the whole
  question, and without being told which half is theirs, both decline —
  measured 4/4 before `SPLIT_SCOPE_PROMPT` existed.
- Specialists do not share a context. What each may *read* is decided as
  deliberately as what each may call — see `app/graph/state.py`.
