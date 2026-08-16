# marq-agent — handoff

State as of commit `e88f708` on `dev`, 13 August 2026.
Read this first in a new session; it replaces having the previous conversation.

---

## What this is

A read-only conversational layer over the MyTAI CRM (PostgreSQL). A supervisor
routes each question to a domain specialist; the specialist reaches data through
one guarded tool and never writes SQL itself.

```
__start__ -> supervisor -+-> deals_agent  -> __end__
                         +-> leads_agent  -> __end__
                         +-> out_of_scope -> __end__   (direct reply, no query)
```

Per turn: 2–3 model calls — route, domain agent, and (for data questions) the
SQL agent inside `sql_query`.

**Request path:** question -> supervisor -> domain agent (ReAct, max 16 steps)
-> `sql_query` -> SQL agent -> `SQLGuard` -> `SQLExecutor` -> PostgreSQL, rows
back up.

## Layout

| Path | Role |
|---|---|
| `app/graph/supervisor.py` | Routing: prompt, `choose_route`, `parse_route` |
| `app/graph/builder.py` | `build_graph(domain)`, `build_supervisor_graph()`, `make_domain_node` |
| `app/graph/agents/domain.py` | `Domain` dataclass, `DOMAINS`, `build_domain_agent` |
| `app/graph/agents/deals.py` · `leads.py` | Agent system prompts only |
| `app/graph/state.py` | `AgentState` = messages + optional `route` |
| `app/sql/catalogue.py` | Schema, business rules, relationships, enums — **and the guard's allowlist** |
| `app/sql/agent.py` | SQL agent; returns `Sql \| Refused` |
| `app/sql/guard.py` | `SQLGuard(tables=...)` |
| `app/tools/sql.py` | `sql_query` — the only path to CRM data |
| `app/tools/analysis.py` · `leads.py` | Arithmetic only; never touch the database |
| `scripts/generate_fixture.py` · `schema_types.py` | Generate the test database from the catalogue |
| `evals/` | `cases.py` (SQL), `graph_cases.py`, `routing_cases.py` |

## Key design decisions (do not undo without reason)

1. **`Domain` binds four things** that must agree: tables shown to the SQL agent,
   tables the guard permits, rules/relationships in the prompt, and the agent's
   own prompt. `build_domain_agent()` derives the whole stack from it.

2. **Domains are asymmetric on purpose.**
   `DEALS` sees deals + leads + users. `LEADS` sees leads + users only.
   Any question involving deals routes to `deals` — it is the only domain that
   can join both. Unparseable routing falls back to `deals` for the same reason.
   The leads guard rejects `deals` even via JOIN or EXISTS.

3. **Routing, not handoff tools.** Agents are sealed by their guards, so a
   misrouted question cannot be rescued mid-answer. One classification call up
   front, written to `AgentState.route`, visible in the trace.

4. **The catalogue is the single source** for both the model's schema view and
   the guard's allowlist, so they cannot drift.

5. **Refusal is a typed outcome** (`Refused`), not an error. The SQL agent emits
   a `CANNOT_ANSWER:` sentinel.

6. **Failures are typed** with `retryable` + `reason`
   (`not_available` / `rejected_by_guard` / `error`). Only the exception *type*
   crosses the boundary — driver errors used to leak DSN fragments.

7. **Rules are composed per table** by `build_rules(tables)`, so the leads agent
   never carries deals rules.

## Verify (all currently green)

```bash
pytest                          # 108 hermetic, <1s
pytest -m integration           # 77, needs live model + PostgreSQL
python -m evals.run             # 32 SQL cases
python -m evals.graph_cases     # 15 whole-graph cases
python -m evals.routing_cases   # 23 routing cases
ruff check .
langgraph dev                   # Studio: marq_agent + both domain graphs
python scripts/generate_fixture.py         # regenerate test DB (byte-stable)
python scripts/generate_fixture.py --stats
```

Fixture: 40 users, 1000 leads, 350 deals. Dates are emitted as
`CURRENT_DATE ± INTERVAL`, so the file is byte-stable but always correctly
positioned in time. Load with `psql ... -f tests/fixtures/deals.sql`.

## Working practice that mattered

- **Verify answers against SQL**, not just that nothing crashed. Most bugs found
  were confident wrong numbers, not exceptions.
- **Three eval layers exist because each catches what the others cannot**: SQL
  cases can't see the agent; graph cases can't see routing.
- **Prompt edits have non-local effects.** Editing one domain's rules has twice
  broken a case in the other. Run all three eval suites after any prompt change.
- **Do not put database values in prompts.** Doing so once (`315/884 = 35.63%`)
  broke the agent outright and would go stale.
- **Check the assertion before believing a failure** — several "failures" were
  bad eval assertions, not bad answers.
- Changes carry `[claude]` comments with reasoning inline.

## Recurring bug family: denominators

Five variants found, all producing a confident wrong number rather than an error:

1. Subset percentage via `calculate_share` — summed 225 + 245 to 470
2. `GROUP BY status` while also filtering status
3. Rate with the measured condition in `WHERE` — every rate came out 100%
4. Share-of-numerator instead of rate-per-group
5. Conversion counting deals instead of leads-that-converted (35.63% vs 31.67%)

Fixed by catalogue rules; expect new variants. A stronger model for the SQL
agent is the alternative lever.

## Open items

**Known bugs (unfixed):**
- Conversion by response speed reports share-of-converted, not conversion rate
  (says 0.11% / 99.89%; truth 6.67% fast / 8.86% slow).
- "Franchises to worry about" invents a "stale deals" metric — deals have no
  staleness concept.
- Pronoun-scope fix has no eval; the graph harness does not support multi-turn
  history.

**Accepted decisions (not oversights):**
- Masked columns are enforced by catalogue omission + prompt, **not** by the
  guard. `SELECT *` is not blocked. Fine on the fixture (columns absent); a real
  gap against the live `mytai` schema. Mentor's call.

**Not built:**
- Row-level visibility (`MyDealsScope` / `MyLeadsScope`). `deal_percentages` and
  `lead_metas` are not in any allowlist, so a scope predicate cannot currently be
  expressed. Decide: inject after validation, or PostgreSQL RLS.
- Lookup tables (`lead_stages`, `projects`, `franchises`, `lead_sources`). Ids
  are queryable, names are not.
- `app/api/`, `app/auth/` — empty placeholders.
- Database role is not read-only. `marq_agent_ro` exists but is unused; the guard
  is currently the only enforcement point.
- `settings` and `app_db` are built at import. This is why the test suite needs a
  session-scoped event loop, and it will resurface when the API layer needs a
  lifespan.

**Housekeeping:**
- Git identity is auto-derived (`ahmedbadr@MacBook-Air-Ahmed.local`) — set
  `git config --global user.email` before pushing.
- `docs/marq-agent-architecture.pdf` is untracked; decide whether generated
  artefacts belong in the repo.

## Adding a third agent

```python
NEW = Domain(
    name="opportunities",
    tables=(OPPS_TABLE, USERS_TABLE),
    system_prompt=OPPS_AGENT_SYSTEM_PROMPT,
)
```

Register in `DOMAINS`. The graph grows a node and a branch automatically. The one
manual step is teaching the supervisor prompt the new category — and adding cases
to `evals/routing_cases.py` at the same time.
