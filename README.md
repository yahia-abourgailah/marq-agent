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
  llm/                 chat model client (OpenAI-compatible, points at vLLM)
  db/
    connection.py      async connection pooling
    repositories/      read-only gateways; every query passes SQLGuard
  sql/
    catalogue.py       the schema contract shown to the SQL Agent
    agent.py           natural language -> one read-only SELECT
    guard.py           query-safety validation
    executor.py        runs validated SQL
  tools/
    sql.py             the `sql_query` tool — the only path to CRM data
    deals.py           scalar analysis tools (no data retrieval)
  graph/
    agents/            conversational agents
    builder.py         wiring
scripts/               developer utilities, not imported by the app
tests/
```

`app/api/`, `app/auth/`, `app/graph/supervisor.py` and `app/graph/agents/leads.py`
are placeholders for work in progress.

## Running the tests

Integration tests need a live model endpoint and a live PostgreSQL, so they are
excluded by default (configured in `pyproject.toml`).

```bash
pytest                  # unit tests only — hermetic, no network or database
```

```bash
pytest -m integration   # the ones that need live infrastructure
```

```bash
pytest -m ""            # everything
```

## Trying the agent in LangGraph Studio

Studio talks to a local LangGraph API server. Start it:

```bash
langgraph dev
```

That reads `langgraph.json`, serves the graph as assistant `deals_agent` on
`http://127.0.0.1:2024`, and opens Studio in the browser. Add `--no-browser`
to skip that.

Prerequisites: PostgreSQL running with the fixture loaded (see Scripts below),
and `MODEL_BASE_URL` / `MODEL_API_KEY` reachable in `.env.development`.

Things worth asking it:

- `How many active deals do we have?` — one `sql_query` call
- `How many contracted deals, and what percentage of active deals is that?` —
  chains `sql_query` into `calculate_percentage`
- `Show me the top 5 deals by area` — exercises the varchar cast
- `What is the total contract price?` — should decline; the column is masked

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

Check that the configured database is reachable:

```bash
python scripts/check_db_connection.py
```

## Linting

```bash
ruff check .
```
