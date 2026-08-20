# marq-agent — handoff

State as of `dev`, 18 August 2026 (API layer added later the same day).
Read this first in a new session; it replaces having the previous conversation.

---

## What this is

A read-only conversational layer over the MyTAI CRM (PostgreSQL), plus a
workspace for files the user uploads. A supervisor routes each question to a
domain specialist; the specialist reaches CRM data through one guarded tool and
never writes SQL itself.

```
website front end
      |  HTTPS + JWT bearer
      v
FastAPI (app/api)  -- auth, identity, limits, SSE
      |
__start__ -> supervisor -+-> deals_agent      -> __end__
                         +-> leads_agent      -> __end__
                         +-> workspace_agent  -> __end__
                         +-> out_of_scope     -> __end__   (direct reply, no query)
```

Per turn: 2–3 model calls — route, domain agent, and (for data questions) the
SQL agent inside `sql_query`.

**Request path:** question -> supervisor -> domain agent (ReAct; 16 steps by
default, 28 for workspace — see `Domain.max_steps`)
-> `sql_query` -> SQL agent -> `SQLGuard` -> `SQLExecutor` -> PostgreSQL, rows
back up. The workspace agent additionally reads uploaded files through its own
tools, and compares the two sides in Python.

## Layout

| Path | Role |
|---|---|
| `app/graph/supervisor.py` | Routing: prompt, `choose_route`, `parse_route` |
| `app/graph/builder.py` | `build_graph(domain)`, `build_supervisor_graph()`, `make_domain_node` |
| `app/graph/agents/domain.py` | `Domain` dataclass, `DOMAINS`, `build_domain_agent` |
| `app/graph/agents/deals.py` · `leads.py` · `workspace.py` | Agent system prompts only |
| `app/graph/state.py` | `AgentState` = messages + optional `route`, `workspace_id`, `requester_id` |
| `migrations/` | `001` read-only role (applied to dev) · `002` RLS (written, **not applied**) |
| `app/sql/catalogue.py` | Schema, business rules, relationships, enums — **and the guard's allowlist** |
| `app/sql/agent.py` | SQL agent; returns `Sql \| Refused` |
| `app/sql/guard.py` | `SQLGuard(tables=...)` |
| `app/tools/sql.py` | `sql_query` — the only path to CRM data |
| `app/tools/analysis.py` · `leads.py` | Arithmetic only; never touch the database |
| `app/tools/workspace.py` | The five workspace tools + `WorkspaceContext` |
| `app/workspace/store.py` | Per-workspace files on disk; isolation and path safety |
| `app/workspace/readers/` | `tabular.py` (xlsx/csv), `documents.py` (pdf + ruled tables) |
| `app/workspace/query.py` · `compare.py` | Exact aggregates; reconciliation. No I/O |
| `app/workspace/chunking.py` · `embeddings.py` · `index.py` | Retrieval side |
| `app/workspace/service.py` | Composes the above; what the tools call |
| `scripts/generate_fixture.py` · `schema_types.py` | Generate the test database from the catalogue |
| `evals/` | `cases.py` (SQL), `graph_cases.py`, `routing_cases.py`, `workspace_cases.py` + `workspace_fixture.py` |
| `scripts/workspace.py` | CLI: upload, list, search, ask, delete |
| `main.py` | **HTTP entry point.** `python main.py`, or `uvicorn main:app` |
| `app/api/app.py` | `create_app()`, the lifespan, CORS, request ids |
| `app/api/routes/` | `chat.py` (JSON + SSE) · `threads.py` · `workspace.py` · `health.py` |
| `app/api/streaming.py` | Graph run -> SSE events, and the token filter |
| `app/api/deps.py` · `errors.py` · `schemas.py` | Dependencies, typed errors, bodies |
| `app/auth/` | `jwt.py` (bearer verification) · `principal.py` (identity) |
| `app/db/state.py` | The **writable** pool — checkpoints and the conversation index |
| `app/db/repositories/conversations.py` | Which threads belong to which employee |
| `app/logging_config.py` | Structured JSON logging; honours `LOG_LEVEL` |

## Key design decisions (do not undo without reason)

1. **`Domain` binds four things** that must agree: tables shown to the SQL agent,
   tables the guard permits, rules/relationships in the prompt, and the agent's
   own prompt. `build_domain_agent()` derives the whole stack from it.

2. **Domains are asymmetric on purpose.**
   `DEALS` sees deals + leads + users. `LEADS` sees leads + users only.
   Any question involving deals routes to `deals` — it is the only domain that
   can join both. Unparseable routing falls back to `deals` for the same reason.
   The leads guard rejects `deals` even via JOIN or EXISTS.

3. **Orchestration, still not handoff tools.** (Revised 19 August 2026 —
   was "routing, not handoff tools".) One planning call up front chooses
   **one or two** specialists, written to `AgentState.plan` with the primary
   still in `route`. They run in parallel and `synthesise` merges them.

   The part worth keeping was never the single agent — it was that agents
   are **sealed**. Orchestration decides who runs, never what they can
   reach: every specialist keeps its own guard and table set, so a
   misrouted question still cannot be rescued by an agent reaching into
   another domain's data.

   What changed is that a question with two halves gets both answered.
   "How do our cancellation rates compare with the market" is a deals
   question and a research question; routing it to either alone returns
   something that looks complete and silently drops half the question.

   One specialist passes through `synthesise` verbatim — no second model
   call, and no paraphrasing of a figure that was verified against SQL. The
   common case costs exactly what it did before.

4. **The catalogue is the single source** for both the model's schema view and
   the guard's allowlist, so they cannot drift.

5. **Refusal is a typed outcome** (`Refused`), not an error. The SQL agent emits
   a `CANNOT_ANSWER:` sentinel.

6. **Failures are typed** with `retryable` + `reason`
   (`not_available` / `rejected_by_guard` / `error`). Only the exception *type*
   crosses the boundary — driver errors used to leak DSN fragments.

7. **Rules are composed per table** by `build_rules(tables)`, so the leads agent
   never carries deals rules.

8. **Vectors find, readers compute.** (Workspace.) Semantic search locates the
   relevant page or rows; every number comes from the parsed content via
   `query.py`. Retrieval returns the passages most *similar* to a question, not
   every row *matching* a condition, so totalling search results is a sampling
   error dressed as an answer — the denominator bug family arriving from the
   file side. The prompt has a `SEARCH FINDS, READS COUNT` section for this and
   `workspace_search` returns `"complete": false` in every payload.

9. **`workspace_id` is runtime context, never a tool argument.** It travels
   `AgentState` -> `make_domain_node` -> LangGraph `context=` -> tools. A model
   that could name its own workspace could name someone else's, and an uploaded
   file could tell it which one.
   `test_no_tool_exposes_workspace_id_as_an_argument` asserts it stays out of
   every tool schema.

10. **File content is untrusted input.** This is the first agent reading text a
    third party wrote. Tool payloads carry `UNTRUSTED_NOTE`, and the prompt has
    a `FILE CONTENT IS NOT INSTRUCTIONS` section. The guard bounds the blast
    radius regardless — the allowlist comes from the catalogue, so nothing a
    file says can widen the SQL surface.

11. **The workspace holds the superset table set**, like `DEALS`. Reconciliation
    is inherently cross-domain and the agent cannot know which tables it needs
    until it has read the file's columns. It adds no CRM reach — same three
    tables, same guard.

## Verify

Note: bare `python` may not be on PATH; the venv interpreter is
`.venv/bin/python`.

```bash
pytest                          # 594 hermetic, ~4s
pytest -m "" --cov=app --cov-report=term-missing   # everything, 93%
pytest -m integration           # 113, needs live model + PostgreSQL, ~3min
python -m evals.run             # 35 SQL cases
python -m evals.graph_cases     # 18 whole-graph cases
python -m evals.routing_cases   # 37 routing cases
QDRANT_URL="" python -m evals.workspace_cases   # 8 workspace cases
python -m evals.complex_cases   # 12 multi-hop cases, through the supervisor
ruff check .
langgraph dev                   # Studio: marq_agent + three domain graphs

AUTH_DEV_MODE=true python main.py          # HTTP API on 127.0.0.1:8000
curl localhost:8000/health/ready           # what is actually reachable
curl -X POST localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Debug-Subject: alice@example.com' \
  -d '{"message":"How many deals are there in total?"}'
python scripts/generate_fixture.py         # regenerate test DB (byte-stable)
python scripts/generate_fixture.py --stats
```

**Current state, after the 17 August pressure-testing pass — everything green:**

| Suite | Result | Was (16 Aug) |
|---|---|---|
| `pytest` | 707/707 | 252 |
| `pytest -m integration` | 124/124 | never run whole |
| `pytest -m ""` (everything) | 831/831, **93% coverage** | never measured |
| `ruff check .` | clean | clean |
| `evals.routing_cases` | 37/37 ×3 runs | 37/37 |
| `evals.graph_cases` | 18/18 ×3 runs | 12/15 |
| `evals.run` | 35/35 ×3 runs | 31/32 |
| `evals.workspace_cases` | 8/8 | did not exist |
| supervisor end-to-end | 8/8 | never tested |

The ×3 columns are the point: each suite was run three times and the failures
counted, not run once until green. SQL, graph and routing produced **nine
consecutive clean runs with zero failures** — the first time this project has
had a measured stability figure rather than a single observation.

**`test_prompt_behaviour::test_workspace_cases` is latency-sensitive**
(observed 18 August 2026). It runs all eight workspace cases sequentially in
one test — by far the most model calls of any single test — and
`get_model()` uses `timeout=30, max_retries=2`, so it is the first thing to
break when the shared vLLM endpoint is slow.

Counted rather than guessed, because it first looked like a change had broken
it:

| `pytest -m ""` | wall time | result |
|---|---|---|
| A | 176s | 1 failed (`APITimeoutError`) |
| B | 166s | 1 failed (`AssertionError`) |
| C | 78s | 831 passed |
| the case alone, ×3 | ~37s each | passed 3/3 |

It tracks **run duration, not code** — the same work varying twofold in wall
time, failing two different ways. A slow run fails it; a fast run does not.
Do not chase this as a logic bug without first checking how long the run
took. If it needs fixing, the lever is splitting it into eight tests or
raising the model timeout, not the prompt.

**The eval suites are not deterministic**, despite `model_temperature=0.0` —
vLLM's continuous batching makes greedy decoding non-reproducible. Re-run
before concluding anything from a single failure, and get a baseline
(`git stash push -u`) before blaming your change.

Measured over three consecutive `evals.run` executions on 17 August, before
the fixes below: 32/34, 34/34, 33/34. Note what that actually showed —
`owner_name_joins_users` failed twice, which is not flakiness but a real
defect hiding behind the assumption of flakiness. **Run a suspected-flaky
case several times and count, rather than re-running until it passes.**

`scripts/`-adjacent helper for this lives in the session scratchpad rather
than the repo; the shape worth keeping is: run every suite N times, print
each run's total, then a failure count per case. A case failing 2 of 3 is a
bug. A case failing 1 of 3 twice in a row is also a bug. Only genuinely
isolated one-offs are the model.

Fixture: 40 users, 1000 leads, 350 deals. Dates are emitted as
`CURRENT_DATE ± INTERVAL`, so the file is byte-stable but always correctly
positioned in time. Load with `psql ... -f tests/fixtures/deals.sql`.

## Working practice that mattered

- **Verify answers against SQL**, not just that nothing crashed. Most bugs found
  were confident wrong numbers, not exceptions.
- **Four eval layers exist because each catches what the others cannot**: SQL
  cases can't see the agent; graph cases can't see routing; none of them can
  see the workspace, which needs a built fixture and a `workspace_id`.
- **Assert on tool names, not just call counts, when the property is *which*
  tool ran.** `workspace_aggregate` and `workspace_search` are both one call
  and only one is right. Conversely, do **not** assert a tool when the case is
  about something else — two workspace cases and one deals case failed correct
  answers because the ceiling or the tool list over-specified the mechanism.
- **Prompt edits have non-local effects.** Editing one domain's rules has twice
  broken a case in the other. Run all the eval suites after any prompt change.
  The exception is a *domain agent's own* prompt, which only its own domain
  loads — check with grep before deciding to skip anything.
- **Do not restate tool docstrings in the system prompt.** The docstrings are
  already in the model's context. The workspace prompt had duplicated the
  `crm_value_columns` example, the result-field glossary and most of the
  comparison detail; removing the duplication took it from 7,918 to 5,025
  characters — the smallest of the three despite having the most tools — with
  no behaviour change. Keep in the prompt only what a docstring cannot say:
  which tool to prefer, what order to work in, and how to behave across a
  whole turn.
- **Trim prompts against the evals, never by eye.** The first pass of that trim
  silently dropped "report all four counts" and the agent started omitting the
  matched count. `evals.workspace_cases` caught it immediately; reading the
  diff had not.
- **Assert the property, not the phrasing.** Roughly half the failures during
  the pressure-testing pass were bad assertions failing correct answers:
  a tool-call ceiling on an open question, a required tool on a question
  answerable another way, `4665` against the agent's `4,665`, a refusal whose
  wording moved when the rule improved, and an `answer_excludes=("stale",)`
  that banned a genuine leads metric once the invented deals one was fixed.
  Anchor on what must be true, never on how it happens to be said.
- **Two columns that describe themselves the same way are a bug.** Not a
  documentation nit: the SQL agent picks one, and both look right. Whenever
  two columns could answer one question, the catalogue has to say which —
  see `agent_id` / `owner_id`.
- **An existing eval is not evidence about the world.** It is evidence about
  what someone previously believed. `owner_name_joins_users` asserted the
  wrong ownership column, and treating it as ground truth propagated that
  error into the catalogue. Check the schema, not the test.
- **Measure coverage instead of guessing where the gaps are.** Four rounds of
  hardening had gone into the workspace while `calculate_funnel` sat at 18%
  and `calculate_share` at 0 — the two tools most likely to produce a
  confident wrong number. Intuition kept pointing at the new code; the
  measurement pointed at the old.
- **Test the entry point people actually use.** Nearly all testing built one
  domain's graph directly, which skips routing entirely. The first end-to-end
  run through `build_supervisor_graph` — the production path — immediately hit
  a step ceiling that single-domain runs never reached. A green suite over a
  path nobody uses is not evidence.
- **A domain's rules must never discuss tables it cannot query.**
  `GENERIC_RULES` goes to every agent and carried a worked example reading
  `FROM deals` — handed to the Leads Agent, whose guard rejects that table,
  which is what `leads_agent_cannot_reach_deals` kept catching. Shared rules
  may only name tables every domain holds (today: leads, users); a
  deals-specific example belongs in the deals rules. Two consistency tests
  now enforce both halves.
  Note the trade: removing that example without replacing it made the deals
  agent *refuse* a contract-rate question it could answer. A worked example
  is load-bearing — move it, don't delete it.
- **Do not put database values in prompts.** Doing so once (`315/884 = 35.63%`)
  broke the agent outright and would go stale.
- **Check the assertion before believing a failure** — several "failures" were
  bad eval assertions, not bad answers. This kept happening: three of the four
  test failures during the workspace build were wrong expectations, including
  one asserting that a similarity search returns *nothing* for an unrelated
  query. It returns the nearest neighbours regardless — that is the whole point.
- **Get a baseline before blaming your change.** `git stash push -u`, re-run,
  compare, `git stash pop`. Both eval suites had pre-existing failures on 16
  August; without the baseline the workspace work would have looked responsible
  for three of them.
- **Run it on a real file.** The workspace passed 240 hermetic tests and then
  broke on the first genuine `.xlsx`, four different ways — see the workspace
  section. Fixtures are written by someone who already knows how the parser
  works; a real export is not. Build the test file out of live data with known
  discrepancies planted, so the expected answer is known before the run.
- **Read the trace, not just the answer.** Two of those bugs were only visible
  in the tool calls. The agent's prose said "several mismatches" and looked
  broadly right while the tool underneath had failed three times and been
  ignored.
- Changes carry `[claude]` comments with reasoning inline.

## The workspace

Uploaded files, and comparing them against the CRM. Added 16 August 2026.

**Two lanes, asymmetric on purpose** — the same principle as `DEALS`/`LEADS`:

| | Spreadsheet (.xlsx .xlsm .csv .tsv) | Document (.pdf) |
|---|---|---|
| Parsed to | typed columns + every row | text per page |
| Answers from | `query.py`, exact | retrieval, cited by page |
| Cited as | `file [Sheet] rows 41-60` | `file, page 4` |

Both are chunked and embedded into Qdrant for search. Only the spreadsheet
lane is queryable for exact values, and that is the distinction the agent must
keep straight — see design decision 8.

**Flow:** `ingest_file` → readers → registry + parsed JSON on disk → chunks →
embeddings → Qdrant. The file is persisted *before* embedding, so a dead
Qdrant or an undownloadable model costs search only; the file still parses,
reads and reconciles, and a warning says so.

**Embedding model:** `paraphrase-multilingual-MiniLM-L12-v2`, 384-dim, local,
CPU. Multilingual because the CRM carries Arabic and English. Its input window
is 128 word-pieces, which is why `DOCUMENT_CHUNK_CHARS` is 700 — longer chunks
are silently truncated by the model, not rejected. Loaded lazily; torch never
imports at module load, and `pytest` never loads it at all.

**Attacked deliberately** on 17 August 2026. Five hostile PDFs — direct
instruction override, forged system authority, an exfiltration request, a
cross-workspace redirect, and a fabricated tool result — were run end to end
through the agent. All five were reported as file content and none obeyed;
`sql_query` was never called on a file's say-so. The case
`injected_instructions_are_reported_not_obeyed` keeps it that way. Two bugs
came out of that exercise, both listed under Fixed below.

**Verified end to end** on 16 August 2026, twice.

First with a synthetic file: English and Arabic queries both retrieved the
right contract pages, exact aggregates matched hand arithmetic, and a search
from a second workspace returned nothing.

Then with a real `.xlsx` built from 40 live CRM rows, with known
discrepancies planted — three areas altered, two rows removed, two invented.
Driven through the actual agent graph, it answered:

    matched 35 · mismatched 3 · only_in_uploaded 2 · only_in_crm 0

which is exactly the planted truth, in one `compare_with_crm` call. The
agent used `workspace_aggregate` for the total area (10,500.5) rather than
totalling search results — the behaviour the whole design exists to produce.

**That run found four bugs the 240 hermetic tests did not.** Worth reading
before trusting the unit suite alone:

1. *A title row became the header.* `_find_header` took the first row with
   anything in it, so a one-cell report title above the real header named
   every column after it and made the file unqueryable. The unit test covered
   leading *blank* rows only. Fixed by taking the first row at least half as
   wide as the widest row in the scan window.
2. *Serving artefacts inside dict keys.* vLLM emitted row dictionaries keyed
   `<|"|>Deal ID<|"|>`, so a correct `key="Deal ID"` did not match — and the
   error printed the mangled names raw, reading as "no column 'Deal ID' …
   available: Deal ID". The agent retried the identical call until it ran out
   of steps.
3. *Column names silently not compared.* `_row_differences` skipped any
   column absent from either side, so an unresolved `value_columns` entry
   meant **nothing was compared and every row came back matched** — a
   reconciliation reporting no problems because it looked for none. Now
   warns, and raises if nothing resolves.
4. *"matched" when nothing was compared.* With no shared columns, every
   common key fell through to `matched`, indistinguishable from genuine
   agreement. `totals` now reports `present_on_both` and
   `"values_compared": false` instead, and omits `matched` entirely.

Bugs 3 and 4 are the dangerous ones: both produce a confident, specific,
wrong reassurance rather than an error — the denominator family in a new
costume.

**Column names are resolved loosely on purpose.** `_canonical` reduces a name
to letters and digits, so `Area (sqm)`, `Area_sqm` and `area sqm` are the
same column. The model rewrites names as it copies them between tool results,
and failing on punctuation would be pedantry rather than safety.
`crm_value_columns` pairs differently-named value fields by position, the way
`key`/`crm_key` already did for identifiers.

**PDF tables reach the exact lane** (added 17 August 2026). A ruled table
inside a PDF becomes a real sheet named `page N table M`, so
`workspace_read_rows` and `workspace_aggregate` work on it with the same
completeness guarantee a spreadsheet gets. A document therefore carries both
`pages` (prose) and `sheets` (its tables).

**Only tables drawn with ruling lines are accepted.** pdfplumber will also
infer tables from text alignment, and on a plain contract that "found" a
table on every page and split words mid-token — `'SCHEDULE A - UN'`,
`'ITS RE'`. Half a table is worse than none because it looks like data, so a
detected table is rejected unless its rows are all the same width, and a
document with no ruled tables refuses row reads and says why. Verified on a
real PDF: total area 1,237 computed exactly, and a planted 205-vs-195
discrepancy caught through `compare_with_crm` — neither possible when the
figures could only be quoted from a search.

**Wired up as of 18 August 2026.** `POST /v1/workspace/files` is the upload
endpoint; it wraps `WorkspaceService.ingest()` without reworking it, exactly as
planned. Ingest runs off the event loop (`anyio.to_thread`) because parsing and
embedding are synchronous and CPU-bound — inline, one upload would stall every
other request on that worker. See "The HTTP API".

## The HTTP API

Added 18 August 2026. This is the layer between the company website and the
agent — the thing `app/api/` was an empty placeholder for.

```
POST   /v1/chat                   ask, wait for the whole answer
POST   /v1/chat/stream            the same turn as Server-Sent Events
GET    /v1/threads                this caller's conversations
GET    /v1/threads/{id}           replay one
DELETE /v1/threads/{id}           forget one, messages and all
POST   /v1/workspace/files        upload
GET    /v1/workspace/files        list
DELETE /v1/workspace/files/{id}   delete
GET    /health                    liveness, no I/O, no token
GET    /health/ready              what is actually reachable
```

**Identity comes from the token and nowhere else.** This is the layer the
handoff kept deferring `requester_id` and `workspace_id` to, and both are now
closed:

| | Where it comes from |
|---|---|
| `requester_id` | the JWT subject claim, verified at the edge |
| `workspace_id` | `sha256(subject)[:32]`, derived — never sent by the caller |
| checkpointer `thread_id` | `Principal.thread_key()`, namespaced per subject |

Three decisions inside that worth not undoing:

1. **The request models set `extra="forbid"`.** Sending `workspace_id` in the
   body is a 422, not a silently ignored field. An ignored field returns 200
   with an answer computed from the caller's own identity, so the front end
   concludes it works and the mistake surfaces much later as a user seeing
   data they should not.

2. **The workspace id is hashed, not the subject itself.** `WorkspaceStore`
   validates against `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`, and a realistic
   subject (`user@example.com`, `auth0|abc123`) fails that pattern — so the
   naive version breaks for real tokens, and only for some users. It also
   keeps email addresses out of directory names, backups and stack traces.
   One workspace per employee; a user with several would need an ownership
   table.

3. **Thread ids are namespaced, not checked.** There is no path that forgets
   the check because there is no unnamespaced key. Two employees may both
   call a thread "today".

**The stream filter was measured, not assumed.** Streaming the raw event feed
of "How many deals are there in total?" emits tokens from two sources the user
must never see:

    node='supervisor'  ->  'deals'                              the route
    node='model'       ->  'SELECT count(*) AS deals_count ...' the SQL agent

`model` is *also* the node the final answer comes from, so filtering by node
name alone shows the user generated SQL. What separates them is the tool
boundary — the SQL agent runs between `on_tool_start` and `on_tool_end`. The
rule is therefore "emit only when not inside a tool and not in the
supervisor", and with it that question streams exactly
`There are 315 deals in total.`

**Verified end to end against the real stack**, not only against stubs:

- `/v1/chat` answered "315 deals", matching `SELECT count(*) FROM deals WHERE
  deleted_at IS NULL` exactly.
- A conversation **survived a process restart** — asked "what number did you
  just tell me?" in a brand-new process, it recalled 315 from PostgreSQL.
  This is the thing `InMemorySaver` could not do.
- A real `.xlsx` built from 12 live CRM rows uploaded, superseded a previous
  upload of the same name, and the agent answered "12 rows, 2,764 sqm" — the
  exact total — using `workspace_aggregate` rather than search.
- Cross-user isolation: a second subject saw no threads, no files, and got
  404 on both.

**Two bugs the hermetic tests did not catch**, both found by running the real
server. Worth reading, because they are the same shape as everything else in
this file:

1. *The stream died on every request.* Two `on_chain_end` events carry
   `langgraph_node == "supervisor"`, and their outputs are **different
   shapes** — an unnamed inner one returning the bare string `'deals'`, and
   the node itself returning `{'route': 'deals'}`. The handler assumed the
   dict and called `.get()` on a `str`. The stub emitted the shape I believed
   in rather than the shape that exists, so 19 tests passed and the first
   real request failed. The stub now emits **both** events, and reverting the
   fix fails three tests.

2. *`Database` could not be reopened.* `close()` set `_opened = False` under a
   comment reading "allow reopening after close" — but a psycopg pool is
   single-use and raises `PoolClosed` on any later use. The flag was reset
   while the pool underneath stayed dead. Nothing had hit it because `app_db`
   is a module-level singleton production opens once; it broke the moment the
   boot test started the application twice in one process. `connect()` now
   rebuilds the pool. **A comment claimed a capability the object did not
   have** — the same failure as `marq_agent_ro`, which existed and could read
   nothing.

**Conversation storage is split in two on purpose.** The checkpointer holds
the messages; `conversations` (migrations/003) holds ownership and metadata.
The checkpointer's tables are keyed by `thread_id` with no notion of an
owner, so it can answer neither "which conversations does this employee
have?" nor "does this employee own this thread?".

**The writable pool is not `app_db`.** `app_db` connects as the read-only role
when one is configured, which cannot create the checkpoint tables. Handing it
the checkpointer would fail on any environment that configured itself
correctly and work on a developer machine that had not — the security posture
and the deployment punished for it exactly inverted. See `app/db/state.py`.

**Answers carry their SQL** (added 19 August 2026). Every chat response has
a `provenance` list holding the query behind it, the row count it matched,
whether it was truncated, and the refusal reason when no query ran.

This is the feature the whole project's working practice implies. "Verify
answers against SQL, not just that nothing crashed" was advice only a
developer with a psql prompt could follow; the number a user saw was
unfalsifiable to that user. Now it is checkable by whoever is reading it.

Captured through a `ContextVar` in `app/sql/provenance.py`, not by adding
`"sql"` to the tool payload. The payload route was rejected twice over: it
spends context tokens on a string the agent wrote and will never read back —
`app/tools/sql.py` already caps that payload because width is what blows the
window — and it invites the agent to quote SQL at users, which the prompts
work hard to prevent. The ContextVar keeps it entirely out of band, and
records nothing at all when nobody is collecting, which is why the CLI, the
evals and the hermetic suite are unaffected.

The mechanism rests on `asyncio.Task` copying its context at creation and on
the collector being mutated rather than rebound, so appends made inside
LangGraph's own tasks reach the request that started them.
`test_records_survive_nested_tasks` pins it: if that stopped holding,
provenance would come back silently empty — present-looking and reporting
nothing.

`EXPOSE_PROVENANCE=false` withholds the field.

**Provenance is kept with the conversation** (19 August 2026).
`migrations/004_conversation_turns.sql` stores one row per completed turn,
so reopening a conversation still shows the SQL behind each answer —
without it a replayed number was back to being one the reader had to trust,
which is the thing the feature exists to prevent.

The turn index comes from the same statement that increments
`turn_count`, inside one transaction: a turn counted without its provenance
would shift every later index by one and pair answers with the wrong
queries, which is worse than showing none. The foreign key cascades, so
deleting a conversation cannot leave the SQL it asked of the CRM orphaned
on disk.

**A local UI** (added 19 August 2026). `app/api/static/index.html`, served at
`/` by this API and built on The MarQ Communities brand — Playfair Display and
Montserrat, the ivory/black split, gold rules, the Marquise diamond.

One static file, no build step, no npm. Served by the API on purpose: that
makes it same-origin, so it works with `CORS_ORIGINS` empty and cannot be
broken by a missing entry — the one configuration gap left open.

It streams over SSE, uploads by drag-and-drop, lists and replays
conversations, and puts the `provenance` SQL one click under every answer,
which is where that feature belongs: a number a user can check rather than
one they must trust. Agent markdown is rendered by a deliberately tiny
escape-first formatter — the text includes CRM rows and, in a reconciliation,
content from a file a third party wrote, so it is never trusted as HTML.

`SERVE_UI=false` removes the route. Off in production, where the front end is
the company website.

Verified in a browser rather than by reading the HTML: a franchise question
answered 12 at 34.62% with its SQL shown, and the reconciliation replayed
17/3/2 against the planted demo file.

**Testing it without a front end.** There is no token issuer yet, so
`scripts/dev_token.py` stands in for one: it keeps a development keypair
under `var/` (gitignored, `0600`) and signs tokens the API verifies exactly as
it will verify real ones. `AUTH_DEV_MODE=true` remains available but is a
*bypass* — it runs no signature, expiry, issuer or audience check, so testing
only that leaves the verification code unexecuted until the website's first
token arrives.

`docs/postman/` holds an importable collection (19 requests, 5 folders) and
`docs/openapi.json` is the exported contract for the front-end team. The
collection doubles as a headless suite — run it with newman; 23 assertions
pass against a live server. See `docs/TESTING.md`.

Two things learned building it:

- *A script named `token.py` breaks the interpreter.* `scripts/` goes on
  `sys.path` ahead of the standard library when a script there runs, so
  `tokenize` — imported by `inspect`, by `dataclasses`, by `cryptography` —
  picked up the new file and died with a circular-import `AttributeError`
  naming `inspect`, which points nowhere near the cause. Renamed
  `dev_token.py`. Same reason `app/logging_config.py` is not `logging.py`.
- *Postman buffers SSE.* It shows one blob at the end rather than live
  frames, so `POST /v1/chat/stream` looks broken there when it is not. Use
  `curl -N`. Documented in the collection itself, at the request.

**Not built at this layer:**

- **No refresh, revocation or key rotation.** Tokens are verified against one
  statically configured key. A JWKS endpoint with caching is the usual next
  step and is not here.
- **No rate limiting.** A single caller can occupy every worker with slow
  model calls. `redis` is already a dependency and is still imported by
  nothing.
- **`/v1/chat` has no timeout of its own.** A turn is bounded only by the
  step ceiling and the model client's own 30s-per-call timeout.
- **The workspace is one per employee.** No sharing, no team workspaces.
- Retention is still unaddressed — see the workspace section. Uploads now
  arrive over HTTP, which makes it more pressing rather than less.


## Complex questions — a fifth eval suite

`evals/complex_cases.py`, added 19 August 2026. Twelve multi-hop,
comparative and rate-based questions, run through
`build_supervisor_graph()` — the production path, which the other graph
suite skips.

**12/12 after the fixes below**, three runs each. It first ran at 8/12, and
the four failures were deterministic defects rather than flakiness — the most
useful thing a new suite can produce.

Half the first run's failures were **bad assertions of mine**, which is the
ratio this file already predicts. Worth recording, because two of them were
the same mistake:

- `sla_breaches` and `stale_share` both hardcoded ground truth that ignored
  `merged_into_id IS NULL`. `LEADS_RULES_ONLY` requires that filter for
  questions about unique leads, the agent applied it correctly, and **the
  eval was the thing that was wrong.** Exactly `owner_name_joins_users`
  again: an eval is evidence about what someone believed.
- `worst_rate` demanded the denominator (26) from an answer that correctly
  said "franchise 12, 34.6%". Asserting the parts of a rate the agent chose
  to express as a rate is asserting phrasing, not property.

**Four real defects, all reproducible:**

1. *"Qualified leads" is read as `qualification_status IS NOT NULL`.* That
   includes `pending` and `unqualified` — 786 leads where 482 are actually
   qualified. "Among qualified leads, how many have gone stale" answers
   168 of 786 against a truth of 104 of 482, wrong by 60%. The catalogue
   describes the column as "Lead qualification status" and never says the
   verdict is one of its values.

2. *The merged-duplicate rule is applied inconsistently.* The same agent
   filters `merged_into_id IS NULL` for the SLA question (153 of 786,
   correct) and omits it for a conversion count in the same session (42
   where 38 is right). A rule followed most of the time is harder to
   reason about than one followed never.

3. *`SUM(area)` is refused while `AVG(area)` works.* "What is the total unit
   area across all contracted deals" returns "not available in the CRM",
   which is false — it is 61,053, and the agent computes an average over
   the same varchar column without complaint in the case above. A wrong
   "unavailable" is the mirror of an invented number: it withholds an answer
   that exists.

4. *A filter is silently dropped.* "Contracted **and residential** and area
   above 250" becomes contracted-and-area-above-250: 117 where the truth is
   90. This is the dangerous shape — a larger, entirely plausible number,
   with no error anywhere.

**All four fixed in the catalogue, 19 August 2026**, and the fixes cost two
lessons this file already teaches:

*Widening a rule caused a regression the eval nearly missed.* Telling the
agent "never refuse a total or an average of area" made it answer "what is
the median unit area" **with an average** — 272.13, confidently, without
mentioning the substitution. The case passed, because it only excluded the
true median (270) and not the mean the agent reached for instead. A mean and
a median differ exactly when the distribution is skewed, which is when
someone asks for a median. The rule now says a median is unavailable and must
not be approximated, and the assertion now rejects the average too.

*The residential filter was a two-column ambiguity, not a dropped one.* After
the first fix the agent stopped omitting the condition and started filtering
`selling_type = 'primary'` instead — 98 where the truth is 90, and unstable
between the two. `selling_type` is primary/resale, about new-versus-resold,
and is independent of property type: primary contains both commercial and
residential deals. This is `agent_id`/`owner_id` exactly — two columns that
could answer one question, with nothing saying which. Fixed from **both**
sides, because describing the mapping on `is_commercial` alone did nothing:
the agent never looked there. `selling_type` now says what it is not.

*Two graph cases moved with the merged-duplicate rule.* Broadening it changed
every lead count: "leads created in the last 60 days" went 57 -> 50, and the
conversion rate 31.67% -> 32.19%. Both cases keep the property they were
written for — the window surviving the handoff, and conversion counting leads
rather than deals — but their baselines moved, because 98 of 884 leads are
merged duplicates of each other. **This is a business decision as much as a
technical one:** "leads received" including duplicates is a defensible metric
too. Consistency was chosen over case-by-case judgement, which demonstrably
failed; reverting is one rule in `LEADS_RULES_ONLY`.

**All five suites after the fixes**, three runs each unless noted:

| Suite | Result |
|---|---|
| `evals.complex_cases` | 12/12 x3 |
| `evals.graph_cases` | 18/18 x3 |
| `evals.run` | 35/35 x3 |
| `evals.routing_cases` | 37/37 x3 |
| `evals.workspace_cases` | 8/8 x3 |
| `pytest` | 722/722 |
| `pytest -m integration` | 124/124 |

**One caveat on the suite itself.** `conversion_within_qualified_leads_only`
names `converted_at` explicitly rather than asking "have they converted",
because the fixture's `converted_at` and its deal linkage disagree almost
entirely — 280 leads have a deal, 78 have a conversion date, 30 have both.
`scripts/generate_fixture.py` sets `converted_at` at random instead of
deriving it, so conversion questions have two defensible answers here. Worth
fixing in the generator before anyone benchmarks against them.

## General and research: the agents that hold no keys

Added 19 August 2026.

`general` answers greetings, "what can you do", and chit-chat. It replaced a
canned refusal, which meant the two things people actually open a chat window
with both got "I can only help with MarQ CRM data" — that reads as broken
rather than scoped, and it was the first thing anyone hit in the new UI.

`research` searches the public web through Tavily — market conditions, news,
regulation, a developer's public reputation.

**Neither is a `Domain`, and neither must become one.** A Domain binds a
table set to a guard, and `build_domain_agent` hands every Domain a
`sql_query` tool. These two are the agents most exposed to being talked into
something — one takes arbitrary user chat, the other reads pages written by
strangers — so they are the ones with no database access at all,
structurally rather than by instruction. A consistency test excludes them
from the route/domain equality check and says why.

**What leaves the building.** A search query is logged by a third party.
"Egypt real estate outlook" is fine; "is <client name> creditworthy" is not.
The tool cannot tell them apart, so the prompt carries the rule and the
architecture enforces it: only the Research Agent holds the tool, and it
never sees CRM rows. When a turn needs both, the specialists run separately
and are merged afterwards — the two halves never meet inside the agent that
can talk to the outside.

**What comes back is untrusted**, in the same sense as an uploaded file and
rather more so: a file at least came from the user. The payload carries
`UNTRUSTED_NOTE` and the prompt has a section on it, exactly as the workspace
does.

**A bug this found while being written.** The first `parse_plan` split on
commas unconditionally, so prose became a plan: "This is about leads, not
deals." returned `["leads", "deals"]` — fanning out to two specialists on a
*negation*, and answering with the domain the classifier had just rejected.
Doubling the cost of a turn to give a worse answer, and the same shape as the
order-dependent substring bug `parse_route` was rewritten to fix. A reply is
now a plan only when **every** comma-separated part is a bare route name.

**And one the change caused.** Collect mode first returned `findings` alone,
and everything reading the trace went blank — `tools_used` in the API, the
tool pills in the UI, and every tool-call assertion in the complex suite,
which dropped 12/12 to 0/12. The answer was fine; the record of how it was
reached had vanished. Specialists now write the working to `messages` and the
answer to `findings`, holding back their final message so `synthesise`
provides the one visible answer.

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

### Next up

The three items from the 2026-08-18 mentor review, and where they stand after
the API work:

| | State |
|---|---|
| `InMemorySaver` -> `langgraph-checkpoint-postgres` | **Done.** `open_checkpointer()` in `app/graph/checkpointer.py`; verified by surviving a real process restart. `build_checkpointer()` still returns an InMemorySaver and is unchanged, so the evals and Studio are unaffected. |
| Structured logging | **Half done.** `app/logging_config.py` emits one JSON object per line and `LOG_LEVEL` is finally read — it had been in every `.env` template since the beginning and `extra="ignore"` was swallowing it. Question and answer text are redacted at the formatter, tested. **`stop_reason` is not done.** |
| CI running pytest + ruff on push | **Not started.** No `.github/` at all. `origin` is a real GitHub repo with `main`/`staging`/`dev`, so it is worth doing. |

**`stop_reason` specifically.** `make_domain_node` still degrades a
`GraphRecursionError` into a friendly `AIMessage` and logs nothing, so an
out-of-steps run is indistinguishable from a real answer to anything watching
— including, now, the API. That was tolerable when a human was reading a CLI
trace and is not once a front end is attached: the operator has no way to
tell "the agent ran out of steps" from "the agent answered". The turn should
carry a reason (`completed` / `out_of_steps` / `refused` / `error`) into the
log line the chat routes already emit.


**Fixed 17 August 2026** (each now has an eval case, so it stays fixed):

- *Conversion by response speed reported a share, not a rate.* Fixed by a
  catalogue rule giving the worked CASE-bucketed query and naming all three
  traps: the denominator is the bucket's own leads, the bucket expression
  must be SELECTed or the rates come back unlabelled, and NULL response
  times belong in neither bucket. Now returns 6.67% / 8.86% against a
  ground truth of 1/15 and 77/869, and volunteers the small-sample caveat.
  Case: `leads_conversion_by_response_speed_is_a_rate`.
- *Two eval cases asserted stale row counts.* `GraphCase.expected_from_sql`
  derives the expectation from the database at eval time instead. The
  eval-side form of the rule against putting database values in prompts.
- *Out-of-domain tables sent the agent into a retry loop.* `SQLGuardError`
  was uniformly retryable, so the Leads Agent rephrased four times for a
  table its guard will never permit, then declined. New
  `TableNotAllowedError` subclass is reported non-retryable / `not_available`,
  so it declines immediately. This is what took `evals.graph_cases` from
  12/15 to 15/15.
- *The invented "stale deals" metric* is **not reproducing** and was not
  fixed by anything in this session — "Which franchises should we worry
  about" now answers from cancellation counts, verified exact against SQL
  (franchise 4 = 10/33, 7 = 7/29, 10 = 8/27, 12 = 9/26). Two cases were
  added to hold that: `deals_have_no_staleness_to_report` and
  `vague_franchise_question_uses_real_metrics`.

**Fixed 17 August 2026, second pass — found by pressure testing:**

- *`agent_id` and `owner_id` both called themselves the owner.* The two
  descriptions were `"Current deal owner / agent user ID."` and `"Deal owner
  user ID."`, so the SQL agent joined on whichever it picked — **13 deals for
  one person through `owner_id` against 6 through `agent_id`, the columns
  disagreeing on 307 of 350 rows.** Two confident, different answers to "how
  many deals does X own".

  **Ownership is `agent_id`**, settled against the live MyTAI schema:
  *"agent_id — deal owner — scope axis together with deal_percentages"*, and
  `MyDealsScope` filters on `agent_id = :me`. `owner_id` is another users
  reference with no ownership role.

  Worth knowing how this went wrong: I first resolved it the other way, making
  `owner_id` canonical because `owner_name_joins_users` expected it. **The
  eval was wrong too.** An existing test is not evidence about the world — it
  is evidence about what someone previously believed. Both the catalogue and
  that case are now corrected against the schema document.

- *The invented "stale deals" metric, root-caused at last.* `is_stale` is a
  **leads** column, and the Deals Agent is shown the leads table too — so it
  read the staleness rule as though the column were universal and wrote
  `FROM deals WHERE is_stale = TRUE`. PostgreSQL rejects that with
  `UndefinedColumn`, and the agent *sometimes* went on to report invented
  stale-deal counts per franchise instead of the failure. Fixed in the
  catalogue: the rule now names its table and says deals have no staleness,
  so the invalid SQL is never written and there is no error to fabricate
  around. Cases `stale_deals_is_refused_not_invented` and
  `stale_leads_still_works`.
- *Re-uploading a filename created a duplicate.* Two entries with one name
  stalled the agent — asked what a document said, it stopped to ask which of
  the two was meant. The second upload now supersedes the first, vectors and
  all, and says so.
- *Concurrent uploads lost files.* The registry is a read-modify-write of one
  JSON file, so two simultaneous writers each read the old list and one
  upload vanished — silently, after the user was told it succeeded. Now
  serialised with an flock.
- *Readers saw a half-written registry.* `write_text` truncates before it
  writes, so a reader in that window got `JSONDecodeError` mid-answer. Now
  written to a temp file and `os.replace`d, which is atomic.
- *CSV sheets were named after the storage id.* A delimited file borrows its
  sheet name from the filename, and on disk that is `wf_21b07b...`. The agent
  told users their data was in "sheet wf_21b07b7fe4281016".
- *The false "nothing uploaded".* The workspace agent would sometimes assert
  no files existed without calling `workspace_files` at all, sending users
  off to re-upload something already there.
- *Fabricated results in files were relayed as findings.* A file containing a
  forged tool result got its number restated as a document summary. The
  prompt now requires naming it as fabricated and leaving the number in the
  file.

**Checked against the live schema, 17 August 2026.**
`~/Downloads/leads-deals-schema.md` is an introspection of the real `mytai`
database (leads 5.36M rows, deals 40k). The catalogue was audited against it:

- **No ghost columns.** Every column the catalogue describes exists in the
  real schema — 62 of the real 69 on deals, 104 of 126 on leads.
- **No masked column exposed.** All eleven remain absent by omission.
- `date_ten_percentage` and `last_activity_feedback` added to the restricted
  list to match the reference implementation. Both are *false positives* of
  that system's money heuristic — one is a date, the other is call feedback —
  so they are masked for no good reason and are worth unmasking deliberately.
- **A lead can produce several deals** (thousands do). Now stated in the deals
  rules, because it is the reason a conversion rate built from deal counts
  reads 35.63% where the truth is 31.67%.
- Still open, needing one query against the live database: `lead_stages` has
  16 rows and the schema document lists all sixteen names but **not** the
  id↔name pairing. Without that pairing the names cannot be used, and
  guessing the order would produce exactly the confident mislabelling this
  codebase keeps fighting. `SELECT id, name FROM lead_stages ORDER BY id`
  closes the "lookup tables" gap for stages.

**Fixed 18 August 2026 — mentor review `a575b67..c70d645`:**

Four of the six findings closed. The two remaining are the ones that need
the real CRM connection, and are described under "Waiting on the database".

- *HIGH — restricted columns enforced by the prompt alone.* Flagged in three
  consecutive reviews. `SELECT contract_price FROM deals` passed the guard
  untouched, as did the same name in a WHERE, an alias, an aggregate or a
  CTE — nine columns across three agents resting on the model complying. The
  list is now `RESTRICTED_COLUMNS` in the catalogue **as data**, the guard
  walks the parse tree for it exactly as it does for tables, and the prompt
  text is rendered from the same tuple so the two cannot drift. Fifteen
  smuggling routes are pinned in `tests/test_sql_guard.py`;
  `RestrictedColumnError` is non-retryable, so the agent says "not available"
  once instead of rephrasing.
- *MEDIUM — `parse_route` was order-dependent substring matching.* Any prose
  reply resolved to whichever route name came first in `VALID_ROUTES`, and
  `out_of_scope` could never win — an off-topic question ran a full CRM agent
  instead of declining in one line. Position could not fix it: *"Not a deals
  question — route to leads"* wants the last mention while *"This is about
  leads, not deals."* wants the first. What separates them is the negation,
  so negated mentions are stripped before matching. All four of the
  reviewer's cases pass, the one-word path is unchanged, and unparseable
  still falls back to `deals`.
- *HIGH — `marq_agent_ro` existed only in docstrings.* It existed on the dev
  database as a login role **with no grants at all** — configured-looking and
  able to read nothing. `migrations/001_read_only_role.sql` grants it SELECT
  on exactly the catalogue tables and revokes the rest; the pool connects as
  it when `POSTGRES_READONLY_USER` is set. Verified by probe: reads 350
  deals, every write rejected, cannot create tables or see out-of-catalogue
  tables. `Database.verify_read_only()` checks what the role *can do* rather
  than what it is called, because those two came apart here.
- *HIGH — no requester identity.* The agent-side half is built:
  `requester_id` travels `AgentState` → runtime context → `SQLExecutor`,
  which publishes it as `app.requester_id` via `set_config` inside an
  explicit transaction. `SET LOCAL` scoping is not cosmetic — without it a
  pooled connection would carry one employee's identity into the next
  employee's question. It is not a tool argument, for the same reason
  `workspace_id` is not.

**Fixed 17 August 2026, fourth pass — found by measuring coverage:**

Coverage was measured rather than guessed, and it pointed straight at the
core rather than the new code:

    app/tools/leads.py       18%   calculate_funnel, essentially untested
    app/tools/analysis.py    74%   calculate_share, compare_periods branches

Both are the *denominator surface* — the tools behind the bug family this
project keeps finding. Both are now at 100%, and two real defects came out:

- *A funnel that grows was reported flat.* Stage ids carry no order, and the
  agent is told a conversion above 100% is the giveaway that the sequence was
  invented — but `calculate_funnel` computed those rates and said nothing,
  leaving the whole defence resting on the model noticing a number it had
  just produced. It now returns a `warnings` list naming the stage, so the
  signal survives a prompt edit.
- *The "password-protected" PDF message was unreachable.* `reader.decrypt()`
  reports a wrong password with a falsy return value rather than raising, so
  the `except` around it never fired and encrypted files surfaced pypdf's
  "File has not been decrypted". Accurate and useless: password-protected and
  scanned-images send someone to fix entirely different things.

`pytest --cov=app --cov-report=term-missing` — now 92% overall. The two
modules under 90% that remain (`db/connection.py`, `workspace/embeddings.py`,
both 59%) are real-infrastructure paths only the integration suite reaches,
which is correct.

Also covered for the first time: `make_domain_node` itself
(`tests/test_graph_node.py`) — the out-of-steps degradation every user sees
when something goes wrong, that only the step ceiling degrades while real
bugs still surface, and that the workspace id reaches the agent as context
and never enters the message state.

**Fixed 17 August 2026, third pass — found by testing the real entry point:**

- *The step ceiling was too tight for a reconciliation.* Almost all testing
  used `build_graph(DOMAIN)` directly; production enters through
  `build_supervisor_graph`. Run that way, "does my sheet match the CRM"
  exhausted `MAX_AGENT_STEPS` and degraded to *"I ran out of steps"*, which
  reads as a broken feature rather than a busy one. A ReAct loop spends two
  steps per tool call and the workspace workflow needs four calls before it
  can answer, so 16 left no room to correct a single mistake. `Domain` now
  carries `max_steps`; workspace uses 28. A consistency test asserts each
  domain's ceiling covers its longest workflow with room to retry.
- *A third file encoded the wrong ownership column.*
  `tests/test_sql_agent_queries.py` asserted `OWNER_ID`, alongside the
  catalogue and `evals/cases.py`. Its `FORBIDDEN_COLUMNS` list was also
  missing five of the eleven masked columns, so those tests would not have
  noticed the agent reaching for a lead's budget or a campaign's spend.

## Waiting on the database

Two of the mentor's findings are genuinely blocked, and it is worth being
precise about which half of each is blocked:

| | needs the real CRM | already done |
|---|---|---|
| Read-only role | `CREATE ROLE`/`GRANT` on production `mytai` | the role and grants on the dev fixture, the migration, the config, and honest docstrings |
| Requester identity + RLS | the policies in `002` | the whole agent-side path: `requester_id` → context → `SQLExecutor` → `app.requester_id` |

So the plumbing is finished and the policy is written. What is missing is a
CRM connection (`CRM_POSTGRES_*` is unset; the host **is** reachable).

**The authenticated caller now exists.** As of 18 August 2026 `requester_id`
is the verified JWT subject, published to PostgreSQL as `app.requester_id` on
every query. So the only thing still standing between here and applying
`002_row_level_security.sql` is the CRM connection itself — the identity half
is done and verified end to end.

**Until `002` is applied, every user of this agent can read every row of
every table in the catalogue.** That is stated in `guard.py` as well, rather
than left implied, and a consistency test fails if anyone re-adds a claim
that RLS is in force.

**Known bugs (unfixed):**
- Pronoun-scope fix has no eval; the graph harness does not support multi-turn
  history.
- **PDF numbers can only be quoted, never computed.** There is no exact lane
  for a table inside a PDF, so reconciling figures from one works on a short
  schedule and would silently sample a long one. See "Not built".

**Accepted decisions (not oversights):**
- Masked columns are enforced by catalogue omission + prompt, **not** by the
  guard. `SELECT *` is not blocked. Fine on the fixture (columns absent); a real
  gap against the live `mytai` schema. Mentor's call.
- Uploaded spreadsheets are queried through a fixed vocabulary of operations
  (`query.py`), not by letting the agent write SQL over them. An uploaded file
  has no schema until it arrives, so the guard's central guarantee — an
  allowlist derived from the catalogue — cannot be reproduced for it. Filters
  are data, never parsed expressions, so there is nothing for a hostile
  spreadsheet to inject into. The cost is that only count/sum/avg/min/max with
  filters and one group-by are expressible; joins across two uploaded files are
  not.
- Retrieval quality is not unit-tested. The fake embedder in
  `tests/workspace_support.py` is a token hash, not a semantic model —
  isolation, citation and plumbing are what those tests assert. Whether the
  right passage ranks first belongs in an eval against the real encoder.

**Not built (workspace):**
- **No Qdrant server here.** There is no Docker and no Homebrew formula on this
  machine, so `QDRANT_URL` (`localhost:6333`) is unreachable. `build_index()`
  gained a `path` mode that runs Qdrant's engine embedded and persisted to
  `settings.qdrant_path` (`./var/qdrant`), which is what the real-file test
  used — run with `QDRANT_URL=""` to select it. Embedded mode is
  single-process, locks its directory, and ignores payload indexes, so
  filtering is correct but scans. **Point `QDRANT_URL` at a real server before
  this carries traffic.**
- ~~No upload endpoint.~~ **Built** — see "The HTTP API".
- **No retention or deletion policy.** `WorkspaceService.delete()` exists and is
  never called by anything. Uploads accumulate under `settings.workspace_root`
  indefinitely, and they contain customer data. This needs a decision before
  anything real is uploaded.
- ~~No per-user check on `workspace_id`.~~ **Closed.** The tools still trust
  whatever the graph puts in state, which remains the right boundary for
  them; the API layer now derives `workspace_id` from the verified JWT
  subject and the request models refuse it as a field. See "The HTTP API".
- **Text-aligned PDF tables are still prose.** Ruled tables now reach the
  exact lane; a table held together only by whitespace does not, because it
  cannot be distinguished from ordinary text. See the PDF-tables note in the
  workspace section for why guessing was rejected.
- Encrypted PDFs are rejected; scanned PDFs parse to zero text with a warning
  saying so. No OCR.

**Not built:**
- **Row-level visibility — waiting on the database.** Decided: PostgreSQL
  RLS, not an injected predicate, because a predicate the agent adds is one
  the agent can be talked out of. `migrations/002_row_level_security.sql`
  encodes both scopes — deals via `agent_id` plus `deal_percentages`, leads
  via the `users.parent_id` subtree — and is deliberately **not applied**.
  It needs the CRM connection and an authenticated caller populating
  `requester_id`; applying it before then denies every row to everyone.
  `deal_percentages` not being in the catalogue is fine here: the policy runs
  inside PostgreSQL, where the agent's allowlist does not apply.
- Lookup tables (`lead_stages`, `projects`, `franchises`, `lead_sources`). Ids
  are queryable, names are not.
- ~~`app/api/`, `app/auth/` — empty placeholders.~~ **Both built.**
- **Database role is read-only when configured.** `marq_agent_ro` had no
  grants at all; `migrations/001_read_only_role.sql` fixes that and the pool
  uses it when `POSTGRES_READONLY_USER` is set. Unset, the guard is still the
  only enforcement point — `Database.verify_read_only()` says which.
- `settings` and `app_db` are built at import. This is why the test suite needs
  a session-scoped event loop. It did resurface when the API layer needed a
  lifespan: `app_db` already exists by the time the lifespan runs, so the pool
  is *opened* there rather than constructed there, and closing it exposed the
  pool-reuse bug described under "The HTTP API". Injecting the database
  instead of importing a singleton is still the real fix.

**Housekeeping:**
- Git identity is auto-derived (`ahmedbadr@MacBook-Air-Ahmed.local`), which is
  what the commits on this branch carry. Set `user.email` before pushing
  anywhere that matters — `git config user.email …` for this repo alone, or
  `--global` if the same identity suits every project on the machine.
- `docs/marq-agent-architecture.pdf` is tracked as of `dcc59eb`; the open
  question is whether generated artefacts belong in the repo at all.
- **Live CRM (`10.10.67.77:5432`) is network-reachable from here, but no
  credentials are configured** — `CRM_POSTGRES_*` appears in the `.example`
  files and is unset in `.env.development`. That is the only thing standing
  between here and the `lead_stages` id↔name pairing.

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

Two things the workspace domain needed beyond that, worth knowing if the next
domain is similar:

- `needs_workspace=True` on the `Domain` if it should reach uploaded files.
  Tools that need runtime state cannot be listed in `extra_tools`, because that
  tuple is built at import and they need a service.
- **Routing order is not cosmetic.** The workspace test comes *first* in the
  supervisor's decision list, because a reconciliation question also mentions
  deals and rule 2 would otherwise claim it — answering the CRM half
  convincingly and never mentioning it could not open the file.
