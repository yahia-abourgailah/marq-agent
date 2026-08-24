# 0001 — The guard and the database role are both required

**Status:** Accepted · **Date:** 2026-08-24 (decided 2026-08-12, revised 2026-08-18)

## Context

The agent writes SQL and runs it against a CRM holding real customer data.
Two mechanisms can stop it doing damage: an application-level guard that
inspects the query before it runs, and a database role that lacks the
privilege to do damage at all.

It is tempting to pick one. The guard alone is portable and needs no
database administration. The role alone is unarguable and needs no parser.

## Decision

Both, always, and neither is allowed to be described as sufficient.

The guard (`app/sql/guard.py`) parses the statement and checks it against
allowlists of tables, functions and columns derived from the agent's
`Domain`. The role (`migrations/001`) holds `SELECT` on exactly the
catalogue tables and nothing else.

`Database.verify_read_only()` reports which protections are actually in
force at runtime, by attempting a write inside a rolled-back transaction —
capability, not configuration.

## Alternatives

**Guard only.** A parser is a model of SQL, and the model is never complete.
This one started as a verb denylist, which a `WITH` clause walks straight
past. It is an allowlist now and no bypass has been found — but "no bypass
found" is a statement about the searcher.

**Role only.** A read-only role cannot stop the agent reading the eleven
masked columns, or joining a table outside its domain. Privilege is coarse;
the guard is where domain scoping lives.

## Consequences

- Two things to keep in step, and `Domain` (see [0002](0002-domain-binds-tables-guard-and-prompt.md))
  is what stops them drifting.
- The role is not always configured. Unset, the pool falls back to the
  owning user and the guard is the only enforcement — which is why
  `verify_read_only()` exists and why the health probe reports it.
- A guard rejection can be worse than the query it blocked. Blocking
  `EXISTS` was safe and pushed the agent onto a different query that
  returned 35.63% where the truth was 31.67%. A refusal is a visible cost;
  a confident wrong number is an invisible one. Weigh both when tightening.
