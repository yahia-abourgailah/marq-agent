# 0002 — One `Domain` binds tables, guard scope and prompt

**Status:** Accepted · **Date:** 2026-08-24 (decided 2026-08-13)

## Context

Three things have to agree about which tables an agent may touch: the guard's
allowlist, the schema block in the agent's prompt, and the tables the SQL
agent is told about. When they disagree the failure is quiet — the agent is
told about a table the guard will reject, retries, and declines.

## Decision

`Domain` (`app/graph/agents/domain.py`) is one object holding a name, a table
set, a prompt and a step ceiling. The guard's allowlist and the prompt's
schema block are both *rendered from that table set*. Adding a domain is
adding a `Domain`; the graph grows a node and a branch automatically.

## Alternatives

**Two lists kept in step by review.** Two lists that must agree will
eventually disagree. One list rendered twice cannot.

**A single global allowlist.** Simpler, and it removes the isolation that
makes a misroute survivable — the Leads Agent could reach `deals` whenever
the classifier slipped.

## Consequences

- Domain isolation is structural. A misrouted question gets a refusal, not
  another domain's data.
- The domains are deliberately asymmetric: `DEALS` sees deals, leads and
  users; `LEADS` sees leads and users. That asymmetry decides most routing,
  because only one agent can answer a question spanning both.
- A domain's tables cannot be widened casually — widening them widens the
  guard, which is the point.
- Not everything belongs in a `Domain`. The general and research agents are
  deliberately not domains, because building one would hand a `sql_query`
  tool to the two agents most exposed to being talked into something.
