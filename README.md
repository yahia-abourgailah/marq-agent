# marq-agent

CRM agent for Marq CRM.

## Branching strategy

| Branch    | Environment | Purpose                                              |
|-----------|-------------|------------------------------------------------------|
| `dev`     | development | Default working branch. All feature branches cut from and merged back here. |
| `staging` | staging     | Pre-production validation. Receives merges from `dev`. |
| `main`    | production  | Released code only. Receives merges from `staging`.   |

Flow: `feature/*` → `dev` → `staging` → `main`

Hotfixes: `hotfix/*` cut from `main`, merged to `main`, then back-merged into `staging` and `dev`.

## Environments

Each branch maps to one environment, configured entirely through environment variables.

| Environment | Template                   | Local file          |
|-------------|----------------------------|---------------------|
| development | `.env.development.example` | `.env.development`  |
| staging     | `.env.staging.example`     | `.env.staging`      |
| production  | `.env.production.example`  | `.env.production`   |

Real `.env.*` files are gitignored — never commit secrets. Copy the template for the
environment you are running against:

```bash
cp .env.development.example .env.development
```

`APP_ENV` selects the active environment at runtime. It is read in
`app/config.py` and picks the `.env.<APP_ENV>` file; unset means `development`.

## Layout

```
app/
  config.py            settings, loaded from the APP_ENV-selected env file
  llm/model.py         chat model client (OpenAI-compatible, points at vLLM)
  db/
    connection.py      async connection pooling
    repositories/
      sql.py           SQLRepository — the only path to the database,
                       and it always validates through SQLGuard first
  sql/
    catalogue.py       the schema contract: tables, rules, relationships
    agent.py           SQL Agent — natural language -> one SELECT, or a refusal
    guard.py           SQLGuard — query-safety validation
    executor.py        runs validated SQL
  tools/
    sql.py             the `sql_query` tool — an agent's only path to CRM data
    analysis.py        arithmetic helpers; no database access, no domain
    leads.py           funnel analysis; numbers only, no database access
  graph/
    agents/
      domain.py        Domain definitions + the generic agent builder
      deals.py         the Deals Agent's system prompt
      leads.py         the Leads Agent's system prompt
    supervisor.py      routing: which specialist owns this question
    builder.py         graph assembly
    state.py           conversation state
    checkpointer.py    conversation persistence (PostgreSQL, or in-memory)
  api/
    app.py             create_app(), the lifespan, CORS, request ids
    routes/            chat (JSON + SSE), threads, workspace, health
    streaming.py       graph run -> SSE events, and the token filter
    deps.py            request-scoped dependencies
    schemas.py         request/response bodies — identity is never a field
    errors.py          typed errors; internals never cross the wire
  auth/
    jwt.py             bearer token verification
    principal.py       the authenticated caller; derives workspace + threads
docs/
  TESTING.md           testing the API by hand
  openapi.json         the exported contract, for the frontend team
  postman/             importable collection + environment
  logging_config.py    structured JSON logging
main.py                HTTP entry point
evals/                 behavioural cases; complex_cases.py is the multi-hop suite
scripts/               developer utilities, not imported by the app
tests/
```

`app/api/` and `app/auth/` are placeholders for work in progress.

### Agents

| Agent | Tables | Answers |
|---|---|---|
| `deals_agent` | deals, leads, users | Pipeline, status, closings, owners, unit inventory |
| `leads_agent` | leads, users | Demand, sources, stages, SLA, qualification, conversion |

Each agent's guard permits exactly the tables its prompt describes, so the
Leads Agent rejects a query against `deals` rather than answering it.

### The supervisor

`marq_agent` routes each question to the specialist that owns it, with one
classification call before any agent runs:

```
START -> supervisor -+-> deals_agent -> END
                     +-> leads_agent -> END
                     +-> out_of_scope -> END   (direct reply, no query)
```

The domains are not symmetric, and that decides most routes. DEALS sees
deals, leads and users; LEADS sees leads and users. So a question spanning
both — "which lead sources produce the most contracted deals" — goes to
DEALS, the only agent that can join them. An unparseable classification falls
back to DEALS for the same reason: a misroute there can still be answered.

The chosen route is written to `AgentState.route`, so a wrong answer caused
by a misroute is visible in the trace rather than having to be inferred.

Routing is asserted by `evals/routing_cases.py` — a misroute is quiet, since
the wrong specialist declines politely rather than erroring.

### Adding another agent

A `Domain` binds the four things that have to agree — the tables its SQL
Agent is shown, the tables its guard permits, the rules and relationships in
its prompt, and its own system prompt. `build_domain_agent()` derives the
whole stack from it, so the guard can never permit a table the prompt never
described.

Adding the Leads Agent means declaring one and registering it in `DOMAINS`:

```python
LEADS = Domain(
    name="leads",
    tables=(LEADS_TABLE, USERS_TABLE),
    system_prompt=LEADS_AGENT_SYSTEM_PROMPT,
)
```

No changes to the guard, the SQL Agent, the repository or the tool. The
business rules are already split per table in `catalogue.py`
(`GENERIC_RULES`, `DEALS_RULES_ONLY`, `LEADS_RULES_ONLY`, `USERS_RULES_ONLY`)
and `build_rules()` composes only the ones for the tables in scope.

The supervisor picks up new domains automatically: `build_supervisor_graph()`
adds a node and a branch for every entry in `DOMAINS`. The one thing that is
not automatic is the supervisor prompt, which has to learn the new category
so the classifier knows when to choose it — add a case to
`evals/routing_cases.py` at the same time.

## Running the API

The service the website front end talks to. `app/api/` sits between the
browser and the agent: it authenticates the caller, derives their identity,
and hands the graph a question.

```bash
python scripts/dev_token.py init   # once — generates a development keypair
python main.py                     # 127.0.0.1:8000
```

Interactive docs at `/docs` once it is up. For production, several workers are
safe because conversations live in PostgreSQL rather than in process memory:

```bash
uvicorn main:app --host 0.0.0.0 --workers 4
```

**Testing it by hand — Postman, curl, known-good answers, and what to do when
something fails: [docs/TESTING.md](docs/TESTING.md).** A ready-made Postman
collection lives in `docs/postman/`.

### Endpoints

| | |
|---|---|
| `POST /v1/chat` | Ask a question, wait for the whole answer |
| `POST /v1/chat/stream` | The same turn as Server-Sent Events |
| `GET /v1/threads` | This caller's conversations |
| `GET /v1/threads/{id}` | Replay one |
| `DELETE /v1/threads/{id}` | Forget one, messages and all |
| `POST /v1/workspace/files` | Upload a spreadsheet or PDF |
| `GET /v1/workspace/files` | List them |
| `DELETE /v1/workspace/files/{id}` | Delete one |
| `GET /health` | Liveness — no I/O, no token |
| `GET /health/ready` | What is actually reachable |

### Authentication

The front end authenticates its own users and presents a signed JWT:

```
Authorization: Bearer <token>
```

The employee id is read from the `sub` claim. **It is never taken from the
request body** — that id becomes `app.requester_id` in PostgreSQL and decides
which rows row-level security will return, so it has to be asserted by the
token issuer rather than by the caller. Sending `workspace_id` or
`requester_id` in a request body is a 422, not a silently ignored field.

Configure `JWT_PUBLIC_KEY` (or `JWT_PUBLIC_KEY_PATH`), `JWT_ISSUER` and
`JWT_AUDIENCE`.

There is no token issuer yet, so `scripts/dev_token.py` stands in for one —
it keeps a development keypair under `var/` and signs tokens the API verifies
exactly as it will verify the real thing:

```bash
curl -X POST localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $(python scripts/dev_token.py mint)" \
  -d '{"message": "How many deals are there in total?"}'
```

`AUTH_DEV_MODE=true` additionally accepts an unsigned `X-Debug-Subject`
header. That is a *bypass* — it runs no signature, expiry, issuer or audience
check — so prefer a real token for anything you intend to trust. It is
refused outright when `APP_ENV=production`, where the process will not
start.

### What the caller never chooses

| | Derived from |
|---|---|
| `requester_id` | the verified `sub` claim |
| `workspace_id` | a hash of the subject — one workspace per employee |
| the checkpointer's thread id | the subject plus the client's thread id |

Two employees can both use a thread called `today` and will never see each
other's. A thread id belonging to somebody else reads as 404, not 403 — a
403 would confirm which ids exist.

### Verifying an answer

Every chat response carries `provenance` — the SQL the agent actually ran:

```json
{
  "answer": "There are 225 contracted deals and 70 cancelled deals.",
  "provenance": [{
    "sql": "SELECT status, count(*) AS deals_count FROM deals\nWHERE deleted_at IS NULL AND status IN ('contracted','cancelled')\nGROUP BY status",
    "question": "count of deals by status",
    "rows_available": 2,
    "truncated": false,
    "refused": null
  }]
}
```

Paste that into psql and you get 225 and 70. That is the point: a number the
user cannot check is a number they have to trust, and every defect this
project has found was a confident wrong number rather than an error.

`refused` is set instead of `sql` when the agent declined — a masked column,
a table outside the domain. `truncated` says the agent saw fewer rows than
matched, which is the only signal that a total computed from them might be
wrong.

The queries are captured out of band and never enter the model's context.
Set `EXPOSE_PROVENANCE=false` to withhold the field.

### Streaming

`POST /v1/chat/stream` emits `start`, `route`, `tool`, `token`, then `final`.
A client can ignore `token` entirely and read `final`, which carries the whole
answer — useful for scripts and across reconnects.

The routing decision and everything the SQL Agent generates are filtered out
before they reach the wire. See `app/api/streaming.py`; the filter is derived
from a measured run, not assumed.


## Running the tests

Integration tests need a live model endpoint and a live PostgreSQL, so they are
excluded by default (configured in `pyproject.toml`).

```bash
pytest                  # 707 hermetic tests — no network or database, ~8s
```

```bash
pytest -m integration   # 124 that need live infrastructure
```

```bash
pytest -m ""            # everything — 831 tests, 93% coverage
```

## Trying the agent in LangGraph Studio

Studio talks to a local LangGraph API server. Start it:

```bash
langgraph dev
```

That reads `langgraph.json`, serves three assistants on
`http://127.0.0.1:2024`, and opens Studio in the browser. Use `marq_agent` —
it routes to the right specialist. `deals_agent` and `leads_agent` are
exposed alongside it for debugging one agent in isolation. Add `--no-browser`
to skip opening it.

Prerequisites: PostgreSQL running with the fixture loaded (see Scripts below),
and `MODEL_BASE_URL` / `MODEL_API_KEY` reachable in `.env.development`.

Things worth asking it:

Deals Agent:

- `How many active deals do we have?` — one `sql_query` call
- `How many contracted deals, and what percentage of active deals is that?` —
  chains `sql_query` into `calculate_percentage`
- `Show me the top 5 deals by area` — exercises the varchar cast
- `What is the total contract price?` — should decline; the column is masked

Leads Agent:

- `How many unique leads do we have?` — deduplicates on `merged_into_id`
- `How many leads are in each stage?` — groups by id; stage names are not
  available and it should say so rather than invent them
- `What share of our leads comes from each utm source?` — chains into
  `calculate_share`
- `Show me the funnel for stages 4, then 7, then 12` — `calculate_funnel`
- `How many contracted deals do we have?` — should decline; deals are outside
  this agent's domain

You can also drive it without the UI:

```bash
curl -s -X POST http://127.0.0.1:2024/threads -H 'content-type: application/json' -d '{}'
```

```bash
curl -s -X POST http://127.0.0.1:2024/threads/THREAD_ID/runs/wait -H 'content-type: application/json' -d '{"assistant_id":"deals_agent","input":{"messages":[{"role":"user","content":"How many active deals do we have?"}]}}'
```

Or in process, with no server at all:

```bash
python -c "
import asyncio
from app.graph.graph import graph
print(asyncio.run(graph.ainvoke(
    {'messages': [{'role': 'user', 'content': 'How many active deals do we have?'}]},
    config={'configurable': {'thread_id': 'local'}},
))['messages'][-1].content)
"
```

## Scripts

The local test database is generated from `app/sql/catalogue.py`, so the column
names in the fixture always match the ones the SQL Agent is told to use:

```bash
python scripts/generate_fixture.py
```

```bash
psql "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@$POSTGRES_HOST/$POSTGRES_DB" -f tests/fixtures/deals.sql
```

Inspect the column types it resolves without writing anything:

```bash
python scripts/generate_fixture.py --report
```

Summarise row counts and distributions:

```bash
python scripts/generate_fixture.py --stats
```

Check that the configured database is reachable:

```bash
python scripts/check_db_connection.py
```

## Linting

```bash
ruff check .
```
