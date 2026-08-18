-- [claude] The read-only role the guard's docstring has claimed since day one.
--
-- `guard.py` stated that a PostgreSQL role prevents the application from
-- modifying CRM data, and that RLS decides row access. Neither was true:
-- `marq_agent_ro` existed on the development database as a login role with
-- no grants at all, and the application connected as the owning user. Every
-- guard check was therefore load-bearing rather than the second layer the
-- file described itself as.
--
-- This grants the role exactly what the agent needs and nothing else, so a
-- guard bug becomes a failed query instead of a write.
--
--   psql -h <host> -U <owner> -d <database> -f migrations/001_read_only_role.sql
--
-- Idempotent: safe to re-run.

BEGIN;

-- The role may already exist without grants, which is the state that
-- prompted this file.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marq_agent_ro') THEN
        CREATE ROLE marq_agent_ro LOGIN;
    END IF;
END
$$;

-- Read the schema, and nothing else in it.
GRANT USAGE ON SCHEMA public TO marq_agent_ro;

-- Exactly the tables in the catalogue. Deliberately enumerated rather than
-- `ALL TABLES`: a new table appearing in the database should not silently
-- become readable by the agent, and the catalogue is the list of what the
-- agent is allowed to know exists.
GRANT SELECT ON TABLE public.deals TO marq_agent_ro;
GRANT SELECT ON TABLE public.leads TO marq_agent_ro;
GRANT SELECT ON TABLE public.users TO marq_agent_ro;

-- Withdraw everything else, including anything inherited from PUBLIC.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public FROM marq_agent_ro;

REVOKE CREATE ON SCHEMA public FROM marq_agent_ro;

-- Future tables created by the owner must not become writable either.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLES FROM marq_agent_ro;

COMMIT;

-- [claude] Belt and braces, and deliberately outside the transaction above.
--
-- `ALTER ROLE` needs CREATEROLE plus ADMIN on the role, which the table
-- owner usually does not have — running it inside the transaction aborted
-- the whole migration and applied none of the grants. Table grants only
-- need ownership, so they must not be hostage to a privilege the deployer
-- may not hold.
--
-- Run this line as a superuser if you can. Skipping it costs the extra
-- layer, not the read-only guarantee: the REVOKEs above already remove
-- every write privilege.
--
--   ALTER ROLE marq_agent_ro SET default_transaction_read_only = on;

-- Row-level security is deliberately NOT enabled here. RLS policies need a
-- requester identity to filter on, and the application only started carrying
-- one in the same change as this file. Enabling RLS with no policy would
-- deny every row; enabling it with a permissive policy would look like
-- protection and provide none. See migrations/002_row_level_security.sql,
-- which is written but must not be applied until the CRM connection exists
-- and `app.requester_id` is being set on every transaction.
