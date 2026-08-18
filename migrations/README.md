# Migrations

Applied by hand, in order. Each is idempotent and safe to re-run.

```bash
psql -h <host> -U <owner> -d <database> -f migrations/001_read_only_role.sql
```

| File | Status | What it does |
|---|---|---|
| `001_read_only_role.sql` | **applied to the dev fixture** | Grants `marq_agent_ro` SELECT on exactly the catalogue tables and revokes everything else. |
| `002_row_level_security.sql` | **written, deliberately not applied** | RLS policies scoping deals and leads to the requester. |

## Why 002 is not applied

It needs two things that do not exist yet.

**A CRM connection.** The application reads a local fixture database;
`CRM_POSTGRES_*` is unset.

**A real requester.** The plumbing carries one — `AgentState` → runtime
context → `SQLExecutor` → `app.requester_id` — and nothing populates it,
because there is no authenticated caller. An API layer with real identity
comes first.

Applying it now denies every row to everyone. Applying a permissive version
instead would look like protection and provide none, which is worse. Until it
is applied, **every user of the agent can read every row of every table in
the catalogue** — that is stated plainly in `guard.py` too, rather than
implied to be handled.

## Verifying 001 actually took

The role existed on the dev database as a login role with **no grants at
all** before this — configured-looking and able to read nothing. So check
what it can do, not what it is called:

```bash
psql -h localhost -U marq_agent_ro -d marq_agent_dev -c "SELECT count(*) FROM deals"        # 350
psql -h localhost -U marq_agent_ro -d marq_agent_dev -c "DELETE FROM deals"                 # permission denied
psql -h localhost -U marq_agent_ro -d marq_agent_dev -c "CREATE TABLE x (id int)"           # permission denied
```

`Database.verify_read_only()` does the same check from the application, and
`tests/test_infrastructure.py` asserts the configured role and its real
privileges agree.

## Using the role

```
POSTGRES_READONLY_USER=marq_agent_ro
POSTGRES_READONLY_PASSWORD=...
```

Unset, the pool falls back to the owning user and the guard is the only thing
between generated SQL and the data.
