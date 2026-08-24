# Migrations

Applied by hand, in order. Each is idempotent and safe to re-run.

```bash
psql -h <host> -U <owner> -d <database> -f migrations/001_read_only_role.sql
```

| File | Status | What it does |
|---|---|---|
| `001_read_only_role.sql` | **applied to the dev fixture** | Grants `marq_agent_ro` SELECT on exactly the catalogue tables and revokes everything else. |
| `002_row_level_security.sql` | **written and executed in test, not applied to any live database** | RLS policies scoping deals and leads to the requester, with `FORCE` and `SECURITY DEFINER` helpers. |

## Why 002 is not applied

**One thing is missing, and one used to be.**

*A CRM connection.* The application reads a local fixture database;
`CRM_POSTGRES_*` is unset. This is the only outstanding blocker.

*~~A real requester.~~* **Closed 18 August 2026.** `requester_id` is the
verified JWT subject, published to PostgreSQL as `app.requester_id` on every
query via `SET LOCAL` inside an explicit transaction. The identity half is
done and verified end to end.

Until `002` is applied, **every user of the agent can read every row of every
table in the catalogue** — stated plainly in `guard.py` too, rather than
implied to be handled.

## Applying 002 — the order matters

`002` sets `FORCE ROW LEVEL SECURITY`, and that changes the deployment
sequence rather than just the security posture.

A table's owner bypasses RLS on that table. Without `FORCE`, applying the
migration while the pool is connected as the owner leaves every policy
inert — full visibility, no error, no log line, and a deployment that looks
correct because the migration ran and the policies exist. `FORCE` closes
that, and in closing it makes the owner subject to policies that name only
`marq_agent_ro`.

**So the owner sees zero rows the moment this is applied.** That is a worse
outage than the exposure it fixes, and it is entirely avoidable by doing the
two steps in the right order:

```
1. Set POSTGRES_READONLY_USER / POSTGRES_READONLY_PASSWORD, restart,
   and confirm /health/ready reports "read-only role".
2. Then apply 002.
```

Reversed, the application goes blind between the two steps.

`Database.rls_posture()` names the failure either way, and
`/health/ready` fails on both:

| Verdict | Means |
|---|---|
| `off` | RLS not enabled. The current state. |
| `inert` | Enabled, this connection owns the table, `FORCE` not set. **Silent full visibility.** |
| `blind` | Forced, and no policy covers this role. **Silent zero rows.** |
| `enforced` | Forced, and policies apply. |

## Verifying 002 as the role that will use it

The header of `002` used to claim it was "verified against the development
fixture". It cannot have been: `deal_percentages` does not exist there, so
the deals policy could not have been created — and any check that did run
would have run as the owner, who bypasses the thing being checked.

`tests/test_rls_policies.py` builds the tables in a throwaway schema and
executes the real predicates with `FORCE` on, which is what makes an
owner-run check meaningful. Against a real CRM, check from the seat the
application will occupy:

```bash
PGPASSWORD=... psql -h <host> -U marq_agent_ro -d <db> \
  -c "SELECT set_config('app.requester_id','17',false); SELECT count(*) FROM deals"
PGPASSWORD=... psql -h <host> -U marq_agent_ro -d <db> \
  -c "SELECT count(*) FROM deals"   # no identity -> 0, not an error
```

Two different employees must return different counts. If they return the
same number, the policies are not being evaluated.

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
