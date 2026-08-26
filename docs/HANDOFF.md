# marq-agent — handoff

State as of `dev`, 24 August 2026.
Read this first in a new session; it replaces having the previous conversation.

**Where things stand.** The agent is reachable over HTTP, has a local UI in
the brand — now animated, responsive down to 375px, keyboard-driven and
touch-legal — answers greetings, searches the web, draws charts, keeps the
SQL behind every answer, and reports how each turn ended rather than letting
an out-of-steps run pass for an answer. All suites green. The two things blocking real use
are unchanged and are not code: **no CRM credentials**, and **row-level
security is written but unapplied**, so every authenticated user can still
read every row.

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
pytest                          # 888 hermetic, ~9s
pytest -m "" --cov=app --cov-report=term-missing   # everything, 93%
pytest -m integration           # 141, needs live model + PostgreSQL, ~3min
python -m evals.run             # 35 SQL cases
python -m evals.graph_cases     # 18 whole-graph cases
python -m evals.routing_cases   # 37 routing cases
QDRANT_URL="" python -m evals.workspace_cases   # 8 workspace cases
python -m evals.complex_cases   # 14 multi-hop cases, through the supervisor
ruff check .
./scripts/ci_local.sh              # exactly what CI runs, in a fresh clone
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
| `pytest` | 822/822 | 252 |
| `pytest -m integration` | 132/132 | never run whole |
| `pytest -m ""` (everything) | 954/954 | never measured |
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

No build step and no npm. Served by the API on purpose: that makes it
same-origin, so it works with `CORS_ORIGINS` empty and cannot be broken by a
missing entry — the one configuration gap left open.

It was one static file until 21 August 2026, when it reached 3,038 lines and
the simplification stopped being one. Now three: `index.html` (162 lines of
markup), `app.css`, `app.js` — still hand-written, still no toolchain.

It streams over SSE, uploads by drag-and-drop, lists and replays
conversations, and puts the `provenance` SQL one click under every answer,
which is where that feature belongs: a number a user can check rather than
one they must trust. Agent markdown is rendered by a deliberately tiny
escape-first formatter — the text includes CRM rows and, in a reconciliation,
content from a file a third party wrote, so it is never trusted as HTML.

`SERVE_UI=false` removes the route. Off in production, where the front end is
the company website.

### The 21 August pass — motion, reach, and touch

Still no build step. What changed:

**Motion, vendored.** `static/vendor/motion.min.js` is Motion 13.1.1 — the
library formerly published as Framer Motion — as its own UMD build, copied
from npm and served by this API. Not from a CDN, and that is a privacy
decision before it is a performance one: a `<script src="https://cdn…">`
would announce to a third party, on every page load, that an internal CRM
tool is being used, and would put a public network dependency between an
internal user and an internal service. `test_the_ui_asks_for_nothing_off_this_host`
holds the rule, because the tempting fix for the next library is a CDN tag
and it would pass every other test.

Three things about the integration are worth knowing before changing it:

- **Every animation runs *to* the element's CSS resting state.** So a
  missing library, or reduced motion, means "already there" rather than
  "invisible". Two bugs came out of getting this wrong: the drawer scrim
  had `opacity: 1` only inside the animation, so the Web Animations fill
  reverted it to the stylesheet's `0` and the scrim never appeared at all.
- **The no-animation branch clears inline styles rather than skipping.** A
  finished animation commits its end value inline, which outranks any rule.
  With animation later switched off, nothing cleared it — jump-to-latest
  stayed invisible with every class correctly applied.
- **`afterExit` runs exit callbacks on a timer, not on Motion's `finished`
  promise.** Measured: a 120ms tween still had an unresolved `finished`
  after 700ms. State changes hanging off it never ran, so the jump button
  never set `hidden`, and its "already in this state" guard then blocked
  every later show. Whichever settles first wins.

**A drawer, instead of an amputation.** Below 900px the sidebar was
`display: none` — the app did not adapt to a small screen so much as lose
three of its four features on one, with no control anywhere to bring them
back. Same panel, same brand, moved off canvas, with a focus trap and a
scrim. Breakpoints at 1440 / 1180 / 900 / 560 / 380, plus a landscape-phone
case; `100dvh` rather than `100vh`, because on iOS `100vh` put the composer
below the fold at rest.

**Touch targets grow; marks do not.** A 12px delete cross is right for a
mouse and unusable with a thumb, and scaling it to 44px would mean designing
the panel twice. Under `(pointer: coarse)` an absolutely positioned `::after`
extends what is clickable past what is drawn, occupying no layout space —
and wherever a hit area widens, the gap between controls widens with it, or
neighbouring targets overlap. Verified: nothing interactive below 44px, and
no horizontal page scroll at 375, 768, 1024 or 1440.

**A command palette (⌘K).** Not a power-user shortcut so much as the second
route to everything, which the drawer made necessary: actions that live only
in the sidebar are three gestures away on a phone and unreachable by
keyboard. Fuzzy-matches actions, conversations and suggested questions.

**Stopping a turn.** There was no way to. A misrouted reconciliation is 28
steps, which is a long time in front of an answer you already know is wrong.
Abort is honest about its scope: it stops *this client reading the stream*.
The server finishes and records the turn, so the partial is kept on screen
and labelled rather than deleted — a turn that ran should not vanish because
nobody watched it end.

**Retry and Edit, which do not rewrite history.** Every turn is recorded in
`conversation_turns` with the SQL behind it, and the whole provenance
argument is that the record can be checked rather than trusted. So Retry
asks again as a *new* turn and Edit loads the text back into the composer.
The thread gains a turn; it never loses one.

**Scrolling that lets go.** Autoscroll was unconditional, so scrolling up to
re-read an earlier answer during a turn was undone by the next token.
Reading the conversation while the agent wrote was impossible.

**Charts: texture, not just colour.** The palette was computed to clear 3:1
on both surfaces, which makes each series visible but not *distinguishable* —
to a reader with deuteranopia the burgundy and the green converge, and every
chart identified its series by colour alone. Each series after the first now
also carries a hatch, applied by walking the finished SVG rather than
threading a pattern id through four measured geometry builders. Series 0
stays solid: a chart where everything is hatched is noisier than one where
texture means "this is the other one".

That walk has an ordering constraint that is easy to reintroduce. It must run
**before** the `<defs>` are inserted. The other way round, it reaches inside
the defs it just added and rewrites each pattern's own background rect —
filled with the very palette colour being matched — into a reference to the
pattern it belongs to. A self-referencing pattern is not an error: it renders
as nothing, so the second series vanished while every fill attribute still
read correctly in the DOM. Caught by looking at the screen, not by asserting
on the tree.

Also on charts: **Save PNG** (2x, styles inlined before serialising — a
serialised SVG carries none of the stylesheet, so without that the export
came out with black Times labels on a transparent ground) and **Copy data**
as TSV. The values table gained a caption, row headers and its own
`overflow-x` box, because a long category name used to make the *page*
scroll sideways.

**Screen readers get state, not tokens.** `#announce` speaks "working on it",
"answer ready", "stopped", "that failed". Piping a character-at-a-time stream
into a live region would read the same sentence dozens of times.

### Splitting the page — 21 August 2026

`index.html` went from 3,038 lines to 162 of markup, with `app.css` and
`app.js` beside it. Three things about how they are served are decisions
rather than defaults:

**Registered by name, not mounted.** `/app.css` and `/app.js` are two
explicit routes. Mounting `static` would be the tidier-looking refactor and
would also serve whatever anyone later drops in beside the page — and the
things that get dropped next to a UI are exactly the ones that should not be
public: a design export, a scratch copy, a `.env` somebody was comparing
against. `test_the_assets_are_named_rather_than_mounted` plants such a file
and asserts it stays unreachable; it was checked against a real directory
mount, where it fails.

**All three carry `no-store`.** They only make sense as a set — the page
names the classes, the stylesheet styles them, the script queries them by id.
Cache one and not the others and a browser holds a *mismatched* set, which
does not present as a caching problem: it presents as a layout regression, or
as controls that silently do nothing because the script is addressing markup
that is no longer there. `motion.min.js` is the deliberate exception, being
pinned and the only large file here.

**`app.js` is a classic script at the end of `<body>`, and depends on it.**
It reads `window.Motion` at the top level and queries the DOM at the top
level, with no `DOMContentLoaded` guard. Giving it `type="module"` or
`async` would break both. The file says so at the top.

One thing the split nearly did quietly: `test_the_ui_needs_no_token_but_carries_no_data`
scans the served UI for embedded credentials, and moving 1,900 lines out of
the page did not fail it — it reduced it to scanning 162 lines of markup
while the part where a token would actually get pasted stopped being checked.
It reads all three files now. A test that keeps passing over less and less is
worse than one that breaks, because nothing tells you it stopped working.

The design guidance came from the `ui-ux-pro-max` skill, installed at
`.claude/skills/ui-ux-pro-max` (searchable UX/style/colour data, its own
`scripts/search.py`). Its **structural and UX** output was used. Its colour
and typography recommendations were **discarded**: it proposed a generic
dashboard blue with Fira Code/Fira Sans, which would have replaced the brand
this file spends three sections protecting. The skill's own instructions say
to treat its results as recommendations rather than as instructions that
override repository rules, which is exactly right.

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
- ~~No rate limiting.~~ **Built** — a per-caller ceiling from the 24 August
  review, and as of 25 August its counters live in Redis, so the limit is
  one limit rather than one per worker. See "One limit, not one per worker".
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
| `pytest` | 822/822 |
| `pytest -m integration` | 132/132 |

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

**And the one that made it into a commit.** `findings` used `operator.add`
and is checkpointed, so it accumulated across turns and was never cleared.
The second turn of a conversation saw the first turn's findings still there,
concluded two specialists had run, and merged the previous answer into the
new one — "okay" came back as a list of Egyptian property developers left
over from the question before it.

Every check after building the orchestration was a **single turn in a fresh
thread**, and every one looked perfect. The bug lives only in the second turn
of a shared thread. `messages` accumulating across turns is exactly what you
want, and `findings` looked identical, which is what made the difference
invisible: anything checkpointed with a concatenating reducer needs an
answer to "when does this end". `None` is now the reset signal and the
supervisor sends it at the top of every turn.

**And one more the change caused.** Collect mode first returned `findings` alone,
and everything reading the trace went blank — `tools_used` in the API, the
tool pills in the UI, and every tool-call assertion in the complex suite,
which dropped 12/12 to 0/12. The answer was fine; the record of how it was
reached had vanished. Specialists now write the working to `messages` and the
answer to `findings`, holding back their final message so `synthesise`
provides the one visible answer.

## Charts

Added 19 August 2026. `make_chart` in `app/tools/charts.py`, available to
every CRM domain. The agent states figures it just retrieved; the browser
draws them from a small JSON spec. No matplotlib, no image bytes through the
model, and no chance of the model describing a picture it cannot see.

**A chart is more persuasive than a sentence, which cuts both ways.** The
failure this project keeps finding is a confident wrong number, and a wrong
number drawn as a bar is worse than the same number in prose — a reader
checks a sentence and trusts a picture. Three things follow, and they are
the design:

- every value is labelled on the mark **and** listed in the chart's table
  view, so nothing is readable only as a length;
- the chart sits in the same response as the `provenance` SQL that produced
  the figures, so both are inspectable together;
- the spec is **validated, not trusted** — mismatched label/value lengths, a
  donut of negatives, non-finite numbers and absurd category counts are
  refused, because a chart that renders wrong is far harder to spot than one
  that does not render.

**The palette was computed, not chosen.** The raw brand colours fail three of
the five checks: burgundy sits below the OKLCH lightness band, charcoal has
zero chroma, and gold falls under both the chroma floor and 3:1 contrast.
`#A82F48 · #9E7410 · #00795F · #4A4FA8` are brand-faithful steps that pass
all five on the ivory and white surfaces alike. Slot 1 carries every
single-series chart, which is the common case — a nominal bar chart is one
colour and the title says what it is, so there is no legend.

**Two mistakes worth remembering.**

*The trigger was too loose for one commit.* The prompt invited a chart when a
breakdown "is plainly easier to read as a picture", and the agent immediately
began charting ordinary grouped questions nobody had asked to see. Judgement
about readability is not the trigger; the user asking is.

*Prompt bloat, not the step ceiling.* Adding a 1,240-character CHARTS section
to each domain prompt broke `vague_franchise_question_uses_real_metrics`: the
agent asked about staleness eight times, was refused each time, and exhausted
its budget — "I ran out of steps" on every run. I first raised
`MAX_AGENT_STEPS` from 16 to 20, which fixed nothing, because the budget was
never the constraint. Cutting the section to 381 characters restored 18/18
across three runs at the original ceiling. The rule this file already states
— **do not restate tool docstrings in the system prompt** — is what I broke,
and the docstring already carried the kinds and the arguments.

## A leaked tool call, and why it happened

Reported from a screenshot, 19 August 2026. Asked "give me a graph" after a
research answer, the user saw this where the answer should be:

    <|tool_call>call:make_chart{kind:<|"|>pie<|"|>,labels:[...]}<tool_call|>

Three separate faults, each worth knowing:

1. **The follow-up was misrouted.** "Give me a graph" contains no CRM
   subject, so rule 4 of the supervisor's list sent it to `general` — which
   does not hold the figures the previous turn produced. A follow-up that
   asks you to do something *with* the last answer now outranks that rule
   and stays with the specialist holding the data.

2. **That specialist could not chart anyway.** `make_chart` was only on the
   CRM domains. Both `general` and `research` have it now; it reaches no
   database, no files and no network, so this widens nothing — the
   sealed-agent guarantee is about data surface, and charting has none.

3. **The leak reached the screen at all.** A model that wants a tool it does
   not have sometimes emits the call as *text*. `clean_answer` in
   `app/api/streaming.py` strips tool-call syntax from any answer and
   substitutes a plain apology when a reply was nothing else. The real fix
   is (2); this is the guard for the next time, because the failure is
   silent — nothing raises, the turn "succeeds", and only a person reading
   the screen can tell.

Also fixed while there: the toolless specialists did not propagate their
intermediate messages, so a research turn that plainly searched and charted
reported `tools_used: []`. Same gap collect mode had — the answer was right
and the record of how it was reached was missing.

## Recurring bug family: denominators

Five variants found, all producing a confident wrong number rather than an error:

1. Subset percentage via `calculate_share` — summed 225 + 245 to 470
2. `GROUP BY status` while also filtering status
3. Rate with the measured condition in `WHERE` — every rate came out 100%
4. Share-of-numerator instead of rate-per-group
5. Conversion counting deals instead of leads-that-converted (35.63% vs 31.67%)

Fixed by catalogue rules; expect new variants. A stronger model for the SQL
agent is the alternative lever.

## The 24 August review

Twelve findings across memory, workflow, RAG and session handling. Eleven
were acted on; one was already closed and is recorded below as such, because
a review finding that turns out to be wrong is worth writing down too.

### The one change that closed three findings

`messages` was doing four jobs — model context, UI trace, the `tools_used`
record, and cross-specialist memory — with different audiences, lifetimes and
sensitivities. That single conflation surfaced as three separate defects:

- **M1**: a `sql_query` payload is capped at 20,000 characters and
  `add_messages` accumulates, so four or five data turns filled the context
  window. The overflow arrived as a provider error, was caught as a generic
  specialist failure, and — because the state is checkpointed — became the
  thread's *permanent* state. Every later turn reloaded it and failed
  identically, with nothing in the reply telling the user to start again.
- **A1**: the research agent, which holds the one tool that sends text to a
  third party and is documented as never seeing CRM rows, read those rows out
  of the same shared history.
- **M2**: checkpoint blobs accumulated client names and unit numbers for the
  life of every conversation.

Rendering a tool pill never needed the tool's *result*, only its *name*. So
names travel on a new `trace` channel and payloads travel nowhere — they stay
inside the agent's own run, where the ReAct loop needs them, and are dropped
when it returns. `tools_used_in` was deleted rather than left unused: a
helper that mines the transcript for tool calls is an invitation to put the
payloads back.

The tests for this assert on **the channel**, not on the research agent. The
leak was never a property of that agent; it was a property of the channel
every agent shares.

### Measure, do not estimate

The review said this twice and was right both times. Both numbers it offered
turned out to be wrong in the direction that mattered.

**Chunk size (R1).** The old constants were 700 characters and 20 rows,
justified by a comment naming the right 128-token window and getting the
conversion wrong. The encoder truncates rather than raising, so nothing ever
failed — the tails were simply never embedded. Measured against the real
tokenizer:

    english prose        621 chars = 128 word-pieces
    arabic prose         522 chars = 128 word-pieces
    twenty real rows    2741 chars = 1007 word-pieces

The review estimated 6–8 rows per chunk. Measurement said **two**. On a real
uploaded export, 13 of 15 chunks were over the window and 408 word-pieces
were never embedded; after packing by tokens, zero and zero, at a cost of six
more chunks. Chunks are now packed against the encoder's own tokenizer where
one is available, with measured character limits as the fallback for
hermetic tests. A single row too wide to pack is split across chunks sharing
its locator rather than silently truncated.

**Relevance floor (R2).** The review suggested "around 0.35–0.45" and said to
calibrate. Measured against a real seven-column export:

    topical queries     top hit 0.304 - 0.448
    unrelated queries   top hit 0.024 - 0.172

0.35 would have discarded *"unit area in square metres"* — a question the
sheet answers. The floor is **0.25**, and it lives on the encoder
(`min_relevance_score`) rather than in the search code, because it describes
one model's score distribution and nothing more general. The token-hash fake
used in hermetic tests declares none, which is correct: measured against it
an unrelated query scores 0.45 and a topical one 0.04, so a number calibrated
for the real encoder is meaningless there.

`search` now returns `(hits, below_threshold)`. "No results" and "results,
all too weak" need different sentences from the agent — the second is "your
files do not cover this", which is a useful answer rather than a shrug.

### The rest

| | What changed |
|---|---|
| A2 | `_conversation` filters *then* slices. Taking the last six raw messages and only then dropping tool traffic left the classifier with the current question and nothing else, so a follow-up after a 28-step reconciliation fell through to the `deals` fallback. |
| A3 | Specialist answers reach synthesis as **user-role** content in a fenced block. They are partly derived from pages written by strangers, and the system role gave that text the same standing as our own instructions at the one node that writes the visible answer. |
| A4 | A process-wide `asyncio.Semaphore` on every model call — subclassed onto the model rather than wrapped, because `create_agent` calls it through interfaces this code never sees, and the nested calls are where the concurrency comes from. Plus a fixed-window rate limit keyed on the verified subject. |
| M1 | `trim_messages` by token budget before every specialist call, and `usage_metadata` logged per turn so the ceiling is observed rather than estimated — which the review explicitly asked for. |
| R3 | Identifiers take an exact path before the vector one. `A-1204` and `A-1240` are near-neighbours to a paraphrase encoder; the exact scan returns the row that actually holds the value. This is the review's named interim — hybrid sparse/dense retrieval is the real answer and needs a Qdrant server, which this machine does not have. |
| R4 | `embedding_model` is stamped on every point, and ingestion refuses to add vectors from a different model than the collection holds. A dimension change already failed loudly; a *same-dimension* swap was the silent one. |
| S1 | A turn cap that refuses in words — the failure it replaces was a generic specialist failure on a permanently broken thread. Plus `expired()` for an age-based sweep. |

### S2 — already closed, and worth recording

The review flagged an unbounded client-supplied `thread_id`: "a megabyte-long
`thread_id` is accepted, stored, and indexed." It is not. `ChatRequest` caps
it at 128 characters with a character-class pattern, and did so before the
reviewed commit — verified against a live server, where 1 MB, 200 characters
and `../../etc/passwd` all return 422.

The finding was still worth acting on. The reviewer read `_thread_id()`,
which does no validation, and **nothing asserted the bound anywhere**. A
limit that is true only until someone widens the field is, from outside,
indistinguishable from no limit. It is pinned now.

### The orchestration bug, found by the full test run and fixed

Not a review finding — found on 21 August while verifying the project, and
pre-existing at `9833045`, which was checked in a worktree rather than
assumed.

**The supervisor planned two specialists correctly and then handed each of
them the whole question.** Asked *"how do our cancellation rates compare with
the wider Egyptian market"*, the plan came back `['deals', 'research']` every
time — and the turn ran **zero tools** and declined. Measured 4/4. Isolated
against the deals agent alone: asked *"what is our overall cancellation
rate"* it calls `sql_query` and answers 22.22%; asked the split question it
refuses. The data was there the whole time.

That is the inverse of the failure this codebase usually guards against — not
half an answer presented as whole, but no answer at all when both halves were
obtainable.

**It survived because nothing asserted a two-specialist answer.** The routing
suite checks single-route classification; every complex case ran one
specialist. A supervisor that plans perfectly and then answers nothing is
invisible to `expected_route`, which was right, and to answer assertions,
which did not exist for this shape.

The fix is `SPLIT_SCOPE_PROMPT` in `builder.py`: when the plan holds more
than one specialist, each is told **what its own part is**, that the rest is
already being answered, and not to mention the other half. Two things about
it are worth knowing:

- **Stating the part positively was necessary.** The first version only said
  what a specialist's part was *not* — "answer what your data covers, ignore
  the rest". The deals agent took that correctly; the research agent did not.
  It fixed on the half it must never touch, reported that it could not see
  MarQ's figures, and never searched at all.
- **The research agent's own prompt was the other half of the bug.** It said
  to "answer only the public half and say the internal half is handled
  separately", and the model did the second half and skipped the first. Two
  instructions fought and the disclaimer won. That line now says to answer
  the public half and say nothing about the internal one.

Measured 0/4 before, **5/5 after**, with both `sql_query` and `web_search`
running every time.

`ComplexCase` gained `expected_specialists` and `expected_tools`, and two
cases use them. Both were checked by disabling the fix and confirming they
fail — which caught a third thing: the first version of the second case was
phrased as a conjunction ("what share of leads go stale, *and* what is the
market doing") and passed with the bug present. Two questions joined by "and"
are read as two questions and each specialist answers its own. A *comparison*
reads as one question a specialist can only half answer, and that is the
shape it declines. The case is phrased as a comparison now.

### Not done

A **rolling summary** for long threads — replacing the oldest exchanges with
a short summary rather than dropping them. The review listed it third in
effort order behind trimming, and trimming plus the turn cap closes the
failure it was for: a long thread no longer breaks, and one that has outgrown
itself now says so. The summary would make a long thread *better* rather than
stop it being broken, which is a feature rather than a fix.

**Hybrid sparse/dense retrieval** (R3's preferred form) — needs a Qdrant
server. See "Not built (workspace)".

---

## Production readiness — 24 August

A separate review, scoring the project against what shipping to real CRM data
needs. Four blockers; two were already closed by the workflow review the same
day, and two were new and serious.

### Blocker 3 — `002` would have failed on the day it was applied

An RLS `USING` expression is evaluated with the **querying** role's
privileges. The deals policy read `deal_percentages`, and `001` grants
`marq_agent_ro` SELECT on exactly three tables — deals, leads, users. Applied
as written, every deals query would have raised `permission denied for table
deal_percentages`: a hard failure on the agent's primary table, not a wrong
answer. `app_requester_subtree()` escaped it only by accident, because it
reads `users`, which happens to be granted.

The lookup is now behind `app_holds_deal_split()`, `SECURITY DEFINER` with a
pinned `search_path`. Better than granting the table: `deal_percentages` is
deliberately not in the catalogue, and a grant would make it readable by
anything connecting as that role. A definer function exposes one question and
nothing else.

### Blocker 4 — RLS could be silently inert

A table's owner bypasses RLS unless `FORCE ROW LEVEL SECURITY` is set, and
this README records that an unset `POSTGRES_READONLY_USER` falls the pool
back to the owning user. Applied that way, every policy is inert: full
visibility, no error, no log line, and a deployment that looks correct
because the migration ran and the policies exist.

Measured on the development database: the application connects as `marq`,
which **owns** both tables. That is the configuration this guards against,
and it was the default one.

`FORCE` is set now, and it has a deployment consequence worth knowing before
anyone applies this: it makes the owner subject to policies that name only
`marq_agent_ro`, so **the owner sees zero rows the moment `002` is applied**.
Switch the pool to the read-only role *first*, then apply. Reversed, the
application goes blind between the two steps. `migrations/README.md` carries
the order.

### The claim that could not have been true

`002`'s header said it was "verified against the development fixture".
`deal_percentages` does not exist there, so the deals policy could not have
been created — and any check that did run would have run as the owner, who
bypasses the thing being checked.

`tests/test_rls_policies.py` is the verification it never had: a throwaway
schema, the real predicates, `FORCE` on so an owner-run check means
something. Eight cases covering both blockers, the deals/leads asymmetry,
failing closed on no identity, and the pooled-connection case where a
rolled-back GUC is `''` rather than NULL.

### `verify_read_only()` was called by nothing

Documented in three places as the check that distinguishes a configured role
from a capable one — and the health probe read `app_db.is_read_only`, the
configured flag, instead. The exact substitution this codebase already
learned to distrust. Both it and the new `rls_posture()` are wired into
`/health/ready` now, which fails on an `inert` or `blind` posture.

### CI, on the seventh review that asked for it

`.github/workflows/ci.yml` — ruff and the hermetic suite on every push. It
kept slipping because it looks like it needs infrastructure; it does not.
What it needed was three placeholder environment variables, because settings
are constructed at import and the model client validates its key in its
constructor. Verified by running the suite with only the checked-in template
plus those three: 888 passed.

The integration and eval suites are deliberately **not** in CI. They need a
live endpoint, and that endpoint is non-deterministic — a gate that fails a
correct change because a sampled answer landed differently teaches people to
ignore the gate.

### ADRs

`docs/adr/` — five records for the decisions that shape everything else:
guard-and-role, the `Domain` binding, the nested SQL agent, routing-not-
handoff, vectors-find-readers-compute. Each says what was decided, what it
was decided *against*, and what it costs.

They exist partly to let this file shrink. The review noted the same
reasoning living in `[claude]` comments, in here, and in commit messages —
three places to update, two that drift.

## Operational hardening — 24 August

Four items from the production-readiness checklist, in the order they were
worth doing.

**Credentials are `SecretStr`.** The five credential fields render as
`**********` in `str()` and `repr()`, so a password can no longer reach a
traceback, a debug log line or an `f"{settings}"` by accident. `reveal()` is
the only way out, which makes leaking one a deliberate act rather than an
incidental format. Not a defence against an attacker — anything that can call
`reveal()` can read the value — but those three routes are how credentials
actually escape.

The test for it produced a false positive on its first run: a substring scan
reported the settings repr as leaking `postgres_password`, because on this
fixture that password is a short word which also appears inside
`postgres_db`, `postgres_user` and three other fields. The credential was
masked correctly; the test was matching the wrong occurrence. It only scans
secrets long enough to be distinctive now.

**Metrics on `stop_reason`.** `/metrics`, unauthenticated like the health
probes, counting turns by `stop_reason`/`route`/`streamed`, tool calls by
name, and rate-limit rejections. `stop_reason` was built so an out-of-steps
run stops passing for an answer; logging it made that investigable, counting
it makes it alertable, which is the difference between finding the failure
when you go looking and being told about it.

Every label is a closed set by construction. No subject, thread id, request
id or question text — those are unbounded *and* customer data, and the
redaction rule in `logging_config.py` does not reach a metrics endpoint.
`test_no_label_carries_customer_data` asserts it, which is what keeps the
endpoint safe to leave open. `prometheus-client` was present transitively and
is now declared, because a version bump elsewhere could otherwise remove an
endpoint the monitoring stack is scraping.

**A retention sweep.** `scripts/sweep_conversations.py`, reporting by
default and deleting only with `--apply`. Checkpoints first, then the index
row — the same order `threads.py` uses, because the reverse leaves a
transcript with nothing pointing at it. A script rather than a background
task: it deletes customer-derived data on a timer, and a sweep that runs
because the process booted is a policy nobody chose. Point cron at it.

**Signing-key rotation.** `jwt_public_key` accepts a PEM bundle and
`jwt_secret_previous` covers the symmetric case, so the outgoing key stays
valid while the incoming one takes over. Without a window, changing a signing
key signs every user out mid-question — which is why, in practice, it never
gets changed.

Two details worth keeping:

- **Only a signature failure falls through to the next key.** An expired
  token fails identically against every key, so retrying it is wasted work
  that also replaces the real reason with a signature complaint in the log a
  support request is answered from.
- **No `kid` handling, deliberately.** Selecting by key id needs a published
  id-to-key mapping. Without JWKS, `kid` is a hint from the token about which
  key to trust, and trying each key is equivalent while the algorithm stays
  pinned.

### CI went red on the commit that added the tests

Worth recording, because the mistake is easy to repeat. The CI environment
was verified once by hand — the checked-in template plus three placeholders,
888 passing — and then three test files were added without re-checking. Two
of them assumed a populated `.env.development`. CI failed on a change whose
author had run the whole suite and watched it pass.

Both failures were in `test_secrets.py` and both were the test's fault:

- **Empty is not unset.** Pydantic renders `SecretStr("")` as `''`, not
  `**********` — there is nothing to mask. The test skipped `None` only, so
  it passed locally, where every credential is populated, and failed in CI,
  where the template leaves four of the five blank.
- **The repr scan found a false positive again.** The workflow set
  `MODEL_NAME` and `MODEL_API_KEY` to the same placeholder, so the api key
  was found inside `model_name`. This is the *second* false positive from
  that scan — the first was a short password appearing inside
  `postgres_db` — and the length threshold added for the first does not help
  with the second, which was fourteen characters.

  A length threshold was the wrong fix. What the scan has to ask is *where*
  the occurrence came from: a secret in the repr is only a leak if no
  non-secret field accounts for it. That is what it does now.

`scripts/ci_local.sh` closes the loop: a fresh clone, working-tree changes
overlaid, only the template plus placeholders, no inherited environment. It
does not reinstall dependencies — that needs the network and has never been
what broke.

### The same bug, three times

`dict_row` bit three separate queries — `rls_posture`, `expired` and
`turn_count` all read `row[0]`, which is a `KeyError` rather than the first
column. Each surfaced differently and the last one is the instructive one:

- `rls_posture` raised the first time it ran.
- `expired` looked correct on a fresh database, because a retention sweep
  that matches nothing never unpacks a row. It raised the moment a window
  was chosen that matched a real conversation.
- `turn_count` passed every hermetic test, because `FakeConversations`
  implements it correctly. The fake modelled the contract; only the real
  query was broken.

That last one is worth remembering: a fake that models the contract is
necessary, and is not evidence that the implementation matches it.

## Remediation review — 24 August

A pass over `7dcadba..b2e1126` confirming the two earlier reviews are closed.
Four items came back, and the first is a residual of our own fix.

### The transcript still carried CRM figures — in prose

Splitting the trace out of `messages` removed the tool *payloads*. It did not
remove CRM data, because **the answers contain it**. A reply to "who are our
top clients by area" is prose naming clients, and it sat in the transcript
every specialist was handed on the following turn — including the research
agent, which holds the one tool that sends text outside the company.

Smaller than raw rows. Identical mechanism, identical mitigation: a prompt
rule. And the docstring in `research.py` — *"the two halves never meet inside
this agent"* — was true within a turn and false across turns.

The fix has two halves:

- **Answers carry authorship.** `synthesise` stamps
  `additional_kwargs["specialists"]` on every message it writes.
- **Toolless specialists read their own history only.** `own_history_only`
  keeps the human turns plus answers this specialist produced, and withholds
  everything else.

Three properties are worth keeping in mind:

*A merged answer is withheld from both contributors.* Two halves in one
message cannot be separated afterwards, so it is attributable to neither.

*Unattributed answers are withheld.* Every message written before this
existed carries no attribution — and those are exactly the ones holding
unfiltered CRM prose, so an old checkpoint replays safely rather than
routing around this.

*Domain specialists are not scoped, deliberately.* A domain agent holds
`sql_query` and can fetch any permitted row whenever it likes; withholding an
earlier answer protects nothing and would break the cross-turn follow-ups the
routing rules depend on. The property being defended is **egress**, and only
research has a tool that leaves the building.

Five tests, including one asserting domain nodes are *not* scoped, so the
asymmetry reads as a decision rather than an omission.

### The smaller three

- **Two docstrings pointed at `index.py`** for `MIN_RELEVANCE_SCORE`, which
  moved to `embeddings.py` when the floor became a property of the encoder.
  The pointers did not move with it. Corrected — and this is the small
  version of the "never let a name lie" rule the reviews keep returning to.
- **`.claude/skills/` is now documented** in the README as deliberate team
  tooling rather than reading as accidental, with what it cost (3.7 MB), why
  its nested `scripts/tests/` is not part of this suite, and how to remove it.
  Verified: `pytest` collects nothing from it and `ruff` excludes it.
- **`test_rls_policies.py` is integration-marked**, so the proof that `002`
  works runs where PostgreSQL exists and not on every push. That is correct —
  it needs a database — but it belongs on the pre-deploy checklist rather
  than being assumed covered by a green CI.

## The console signs itself in, and reads like a report — 24 August

Two requests, both about the local console.

### `DEV_UI_TOKEN`

Set it in the environment file and the console picks it up; no minting and
pasting. Served by `/app-config.js` as a one-line script rather than baked
into `index.html`, so the page still leaves disk unchanged — a page templated
at request time is one whose served bytes differ from the file, which is
exactly the difference that makes "it works locally" hard to investigate.

Gated twice, because the failure is silent. The route is not registered when
`APP_ENV=production`, **and** `create_app` refuses to start if the variable
is set there. A `.env.production` that inherited it from a copied development
file would hand a working bearer token to anyone who could load the page, and
nothing about the deployment would look wrong. Same shape as `auth_dev_mode`,
guarded the same way.

**The configured token wins**, and the first version had this backwards.

I made a stored token win, reasoning that somebody who pasted a specific
identity should not be signed back in as someone else by a reload. That was
the wrong default and it failed immediately in the only way that mattered: a
browser used before the feature existed kept a two-day token from an earlier
session and silently ignored the thirty-day one from the environment — which
is exactly the problem the setting was added to remove. The reporter's
console still read `VALID TO AUG 26` while the environment held a token
valid to September.

An explicit override still survives a reload, and it is now explicit rather
than incidental: editing the field records that this browser has been told
to use a particular token, and only then does storage outrank configuration.
Clearing the field revokes that and hands control back — otherwise clearing
it is a dead end, signed out beside a perfectly good configured token with
no way back except knowing to paste again.

When the environment supplies the token, the identity block is **read
only** — no chevron, no click, no drawer, no password field. A field
labelled "paste a bearer token" is a task, and a task already done reads as
one still outstanding; it also invites a user of an internal tool to think
credentials are their problem, which is the opposite of what configuring
`DEV_UI_TOKEN` achieved. The control is hidden rather than deleted:
`⌘K → Change access token` still opens the drawer, because a developer
testing a second identity needs a way in.

### What this is not

This is a **development convenience**, and it should not be mistaken for how
the product authenticates. Two things about it are wrong for production and
deliberately so:

- **The token lives in `localStorage`.** Any XSS on this origin can read it.
  It is acceptable here because the console is a local tool, `SERVE_UI=false`
  in production, and the token is a development one for a fixture database.
- **The identity is a static token in a file.** There is no login, no
  refresh, no revocation.

The production shape is the one `app/auth/jwt.py` was built for and already
supports: the company's identity provider authenticates the user, the front
end obtains a short-lived JWT through OIDC, and this API verifies its
signature. What should change on the way there is *where the browser keeps
it* — an `httpOnly; Secure; SameSite=Strict` cookie set by the server, or
held in memory and refreshed, so that a script on the page cannot read it at
all. `DEV_UI_TOKEN` should never be set in a deployment that has real users;
`create_app` refuses it when `APP_ENV=production` for exactly that reason.

It is a literal token, so it expires — 24 hours by default. `mint --expires
2592000` gives thirty days; when it lapses the console falls back to the
manual field, which is where it started.

### Answers that read like answers

The renderer handled bold, italics, code, bullets and hashes. Asked to
compare franchises, the agent replies with a **markdown table** — and it
matched neither the bullet nor the heading pattern, so every row rendered as
a paragraph of raw pipe characters. That is the most visible thing in an
answer and it read as broken software.

Tables are real tables now, and three details do the work:

- **Numeric columns are detected and right-aligned in tabular numerals.**
  Agents emit `| :--- |` for every column out of habit, so honouring the
  markdown alignment alone left figures ragged-left in proportional digits.
- **The first column is never auto-right-aligned.** "Franchise" holds 9, 5
  and 12 — identifiers, not amounts. A table's stub column is left-aligned
  because that is what makes rows readable, and lining ids up against
  quantities invites comparing them.
- **Its own `overflow-x` box**, so a wide table never makes the page scroll
  sideways.

Also added: ordered lists, and a line that is entirely bold treated as a
subheading, which is what the agent writes above a breakdown.

Still escape-first. Verified with a table whose cells contain
`<img src=x onerror=...>` and `<script>`: nothing is injected, the markup
renders as text, and the markdown inside the same table still works.

## Running it in a container — 25 August

`Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`,
`.dockerignore`, `.env.docker.example`. Multi-stage, non-root, CPU-only
torch, readiness as the HEALTHCHECK.

```bash
cp .env.docker.example docker.env       # fill in MODEL_*, DEV_UI_TOKEN
docker compose --env-file docker.env up -d --build
open http://localhost:8000
```

**It binds 8000, the same port as `python main.py`. Run one or the other.**
An earlier version used 8080 so both could run, and what that produced was
two servers, two databases and two sets of conversations with nothing on
screen saying which you were looking at. For live reload while editing, add
`-f docker-compose.dev.yml`, which mounts the working tree over the image's
copy. It is not named `override.yml` on purpose: auto-loading the mounts
would mean never being able to test the built image.

**The image is 3.2 GB**, ~1.6 GB of it the local encoder plus ~470 MB of
weights. That is the price of embedding uploads in-process rather than
posting them to an API — a privacy decision, not an oversight.
`--build-arg PRELOAD_EMBEDDER=false` gives 2.3 GB and downloads on first
upload.

### Five things only running it revealed

- **The stack came up healthy and every `/v1/threads` was a 500.** The
  compose comment claimed the app created its conversation tables on boot.
  It does not: the checkpointer creates the four `checkpoint_*` tables it
  owns, and `conversations` / `conversation_turns` come from 003 and 004.
  There is a `migrate` service now, applying **only those two** — 001 and
  002 are decisions about a real CRM.
- **Host port 5432 answered from two databases.** The local Postgres and
  the container both bound it, so `psql -p 5432` reached the local fixture
  while appearing to reach the container's. Host ports are offset now
  (5433, 6334, 6380).
- **The containerised console could not sign in at all.** `/app-config.js`
  returned null and everything 401'd. `AUTH_DEV_MODE` does not help — it
  accepts an `X-Debug-Subject` header, which curl can send and a browser
  page cannot. The container mounts `var/dev-jwt/` read-only and verifies a
  real signed token.
- **The preloaded image spent ~40 s on HuggingFace retries per first
  embed**, before falling back to weights already on disk. `HF_HUB_OFFLINE`
  now tracks the build arg: 4.8 s, verified with `--network none`.
- **Two workers raced to create the checkpoint tables.** See below — the
  only one of the five that was a bug in the application rather than in the
  container setup.

### The checkpoint race — not a container problem

`AsyncPostgresSaver.setup()` issues `CREATE TABLE IF NOT EXISTS`, and that
is **not atomic** in PostgreSQL. Two sessions can both pass the existence
check and both attempt creation; the loser fails with a unique violation on
`pg_type_typname_nsp_index`. With two uvicorn workers against an empty
database, both reach `setup()` in the same instant.

It presented as a crash loop that healed itself: one worker raised, uvicorn
stopped the parent, and the restart succeeded because the tables existed by
then. That is the kind of failure that gets written off as noise.

`_setup_once` retries a bounded number of times and treats "somebody else
created it" as success. **The fix is in the application, not the compose
file**, because any deployment starting replicas in parallel against a fresh
database has this and Kubernetes does it by default.
`tests/test_checkpoint_race.py` races four callers at an empty database;
checked both ways, it reproduces the violation with the fix reverted.

## One limit, not one per worker — 25 August

`redis` had been a pinned dependency, a required setting and a declared
compose service since the container work, and was imported by nothing. This
is the thing it was declared for.

The rate limiter from the 24 August review kept its fixed window in a
dictionary, which is correct for one process and quietly wrong for two. The
compose stack runs uvicorn with two workers — the boot log says so twice,
`Started server process [8]` and `[9]` — so a configured ceiling of 30
admitted 60, each worker counting its own half and neither aware of the
other. Nothing about that shows up in a single-process test, which is why it
survived the review that introduced the limiter.

`app/api/ratelimit.py` now has two backends behind one interface.
`build_rate_limiter()` picks on `REDIS_URL`, and the choice is reported in
the boot log as `rate_limit_backend` rather than inferred, because the two
are indistinguishable until the second worker exists — which is exactly when
nobody is looking.

**The window is aligned to the wall clock, and that was forced rather than
chosen.** Two workers can agree on `int(time.time()) // 60` without talking
to each other. They cannot agree on "when did alice first call" without a
round trip to find out, and a limiter that reads before it writes has a race
in it. What changes for a caller: `Retry-After` counts down to the top of
the minute rather than to sixty seconds after their own first request. What
does not change is the guarantee — a runaway tab is stopped inside a minute.
`INCR` still runs on the rejected request, so hammering still buys no reset.

**A cache outage must not become an API outage, and must not become an
unlimited API either.** Every failure — refused, timed out, a `MISCONF` from
a Redis that cannot snapshot, a client exception this module has never heard
of — falls back to the in-memory limiter, which is the behaviour the service
had until today: weaker, still a limit. The transition is logged once per
outage rather than once per request.

**The part that was wrong until it was measured.** The first version had the
fallback and the short socket timeout and looked finished. Against a
blackholed address — packets dropped rather than refused, which is what a
*hung* Redis looks like, as opposed to a stopped one — it paid the full
connect timeout on every single request:

    first call   0.253s
    second call  0.251s

Falling back is only half of surviving an outage; the other half is not
asking again for a while. `REDIS_RETRY_SECONDS` holds the circuit open for
five seconds after a failure, so an outage costs one 250ms probe per five
seconds instead of 250ms per request, and a recovery is noticed within five
seconds of happening. Re-measured after the fix: 252.7ms, then five calls at
0.0ms. This is the same lesson as "Measure, do not estimate" above, and it
is worth noticing that the bug was in the *mitigation* — the code written to
handle a failure was the code that had not been run against one.

Eleven cases in `tests/test_load_limits.py`, against a double rather than a
server: two limiters over one store, which is what two workers are. Verified
against a real Redis as well — `docker compose up -d redis`, two limiters at
a ceiling of three, `[True, True, True, False, False]`.

Not done, deliberately: the fallback's dictionary is per process, so during
an outage the limit is loose by the worker count. Making it exact would need
the workers to coordinate, which is the thing Redis was for.

## Open items

### Next up

The three items from the 2026-08-18 mentor review, and where they stand after
the API work:

| | State |
|---|---|
| `InMemorySaver` -> `langgraph-checkpoint-postgres` | **Done.** `open_checkpointer()` in `app/graph/checkpointer.py`; verified by surviving a real process restart. `build_checkpointer()` still returns an InMemorySaver and is unchanged, so the evals and Studio are unaffected. |
| Structured logging | **Done.** `app/logging_config.py` emits one JSON object per line and `LOG_LEVEL` is finally read — it had been in every `.env` template since the beginning and `extra="ignore"` was swallowing it. Question and answer text are redacted at the formatter, tested. `stop_reason` closed 20 August — see below. |
| CI running pytest + ruff on push | **Not started.** No `.github/` at all. `origin` is a real GitHub repo with `main`/`staging`/`dev`, so it is worth doing. |

**`stop_reason` — done, 20 August 2026.** `make_domain_node` degraded a
`GraphRecursionError` into a friendly `AIMessage` and logged nothing, so an
out-of-steps run was indistinguishable from a real answer to anything
watching — including the API. Every turn now carries one of `completed` /
`out_of_steps` / `refused` / `error` into a `chat_turn_complete` log line,
into `ChatResponse.stop_reason`, and onto the SSE `final` event.

Three things about it are worth knowing before changing it:

- **A specialist never writes the channel; `synthesise` does.** The
  specialists run in one superstep, and two of them writing a plain channel
  is `InvalidUpdateError` — the same collision `findings` carries a reducer
  for. So each specialist records its outcome *inside its finding*, where the
  reducer already handles the concurrency, and one node downstream resolves
  the turn's single reason. `test_two_specialists_in_one_superstep_do_not_collide`
  fails the moment someone "simplifies" that; the collision was reproduced
  before the test was written, so it is guarding a real edge and not a
  supposed one.

- **Worst-wins across specialists.** A turn where deals answered and research
  died reports `error`, not `completed`. The reader still gets the half that
  worked — `synthesise` reports what it has — but a half answer that calls
  itself complete is the exact failure shape this project keeps finding.

- **No value is inferred from the answer text.** Each reason is decided by
  control flow: an exception caught, a branch taken. `refused` is the
  structural case where nothing came back with anything in it, so
  `synthesise` fell through to the canned reply — not a guess that some prose
  sounded like a refusal. An empty answer left behind by an exception is
  labelled `error` at the point it is caught, so the two never collapse.

`stop_reason_from` in `app/api/streaming.py` reads the value off *every*
`on_chain_end` rather than matching `langgraph_node == "synthesise"`, and
that was measured rather than assumed — the same care `route_from` above it
documents. A real run emits two chain-ends carrying the reason:

    name='synthesise'  node='synthesise'  -> completed
    name='LangGraph'   node=None          -> completed

The second is the graph's own final output and has **no node name at all**, so
a reader keyed on the node would have worked by luck on the first and missed
the second. Last one wins. Only the four known values are accepted, so a
future node writing something else reads as "no reason observed" rather than
reaching the front end as a status nobody defined.

The one thing deliberately *not* done: the local UI does not display it. The
field exists for the operator and for monitoring; the reader already sees the
agent's apology in prose.


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
