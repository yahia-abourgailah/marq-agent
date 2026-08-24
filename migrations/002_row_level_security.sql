-- [claude] Row-level security for CRM reads.
--
-- ============================================================
-- DO NOT APPLY THIS YET.
-- ============================================================
--
-- It is written, and it is verified against the development fixture, but it
-- must not run against the real CRM until two things are true:
--
--   1. The application connects to that CRM. Today it reads a local fixture
--      database; `CRM_POSTGRES_*` is unset.
--   2. Every request carries a real `requester_id`. The plumbing exists —
--      AgentState -> runtime context -> SQLExecutor -> `app.requester_id` —
--      but nothing populates it yet, because there is no authenticated
--      caller. An API layer with real identity has to come first.
--
-- Applying it before then denies every row to everyone, which is a worse
-- outage than the exposure it fixes. Applying a permissive version instead
-- would look like protection and provide none, which is worse still.
--
-- ============================================================
-- What it enforces
-- ============================================================
--
-- Reproduced from docs/GLOBAL_SCOPES.md as summarised in the schema
-- document. Deals and leads scope differently, and that asymmetry is the
-- whole point:
--
--   Leads  — MyLeadsScope cascades the reporting tree: a manager sees their
--            subtree via users.parent_id.
--   Deals  — MyDealsScope does NOT cascade. Default visibility is the
--            agent's own deals plus any deal they hold a commission split
--            on (deal_percentages). That split row, not the reporting tree,
--            is how a manager sees a downline deal.
--
-- `deal_percentages` is not in the agent's catalogue and does not need to
-- be: the policy runs inside PostgreSQL, where the catalogue does not
-- apply. That is precisely why row scoping belongs here rather than in a
-- predicate the agent injects — the agent cannot be talked out of a policy
-- it cannot see.
--
-- ============================================================
-- Reading the identity
-- ============================================================
--
-- NULLIF(current_setting('app.requester_id', true), '') — both forms, and
-- the reason is not cosmetic. On a connection that has never carried the
-- setting, current_setting returns NULL; once any transaction has set it,
-- PostgreSQL keeps the GUC defined for the session and a rollback returns
-- it to ''. Connections here are pooled, so both occur. A policy testing
-- IS NULL alone would pass on a fresh connection and fail on a reused one.
--
-- When no identity is set, these policies deny everything. That is the
-- correct direction to fail: a missing requester means an unauthenticated
-- request, which should see nothing rather than everything.

-- ============================================================
-- Why every function below is SECURITY DEFINER
-- ============================================================
--
-- [claude] An RLS `USING` expression is evaluated with the privileges of
-- the *querying* role, not the policy author's. So a policy that reads a
-- table the querying role cannot reach does not deny rows — it raises
-- `permission denied`, on every query against the table it protects.
--
-- The deals policy reads `deal_percentages`, and `001` grants
-- `marq_agent_ro` SELECT on exactly three tables: deals, leads, users. The
-- day this was applied as written, every deals query would have failed with
-- `permission denied for table deal_percentages` — a hard failure on the
-- agent's primary table, not a wrong answer.
--
-- `app_requester_subtree()` only escaped this by accident: it reads `users`,
-- which happens to be granted.
--
-- SECURITY DEFINER is better than granting the table. `deal_percentages` is
-- deliberately not in the agent's catalogue — the agent must not be able to
-- name it — and a grant would make it readable by anything connecting as
-- that role, including a `sql_query` the guard let through. A definer
-- function exposes exactly one question ("does this user hold a split on
-- this deal?") and nothing else.
--
-- `search_path` is pinned on each one. A SECURITY DEFINER function without
-- it runs the caller's `search_path` with the owner's privileges, which is
-- the standard way these become a privilege-escalation route: the caller
-- creates `public.users` in a schema earlier on the path and the function
-- reads theirs instead of ours.

BEGIN;

CREATE OR REPLACE FUNCTION app_requester_id() RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.requester_id', true), '')::bigint
$$;

-- The reporting subtree, for leads. Anchored on the requester and walking
-- users.parent_id downwards.
CREATE OR REPLACE FUNCTION app_requester_subtree() RETURNS SETOF bigint
LANGUAGE sql STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    WITH RECURSIVE subtree AS (
        SELECT id FROM users WHERE id = app_requester_id()
        UNION
        SELECT u.id FROM users u JOIN subtree s ON u.parent_id = s.id
    )
    SELECT id FROM subtree
$$;

-- The commission-split test, for deals. One question, answered without
-- exposing the table it reads.
CREATE OR REPLACE FUNCTION app_holds_deal_split(deal bigint) RETURNS boolean
LANGUAGE sql STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1 FROM deal_percentages dp
        WHERE dp.deal_id = deal
          AND dp.model_id = app_requester_id()
          AND dp.model_type LIKE '%User'
    )
$$;

-- The definer functions run as their owner, so they must not be callable
-- by anything that should not ask the question.
REVOKE EXECUTE ON FUNCTION app_requester_subtree() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION app_holds_deal_split(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_requester_subtree() TO marq_agent_ro;
GRANT EXECUTE ON FUNCTION app_holds_deal_split(bigint) TO marq_agent_ro;

ALTER TABLE deals ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;

-- ============================================================
-- FORCE, and why it is not optional
-- ============================================================
--
-- [claude] A table's owner bypasses RLS on that table. `ENABLE` alone
-- therefore protects nothing against a connection that happens to own the
-- table — and migrations/README.md records that when
-- `POSTGRES_READONLY_USER` is unset the pool falls back to the owning user.
--
-- So without FORCE, applying this migration while that variable is unset,
-- misspelled, or failing to authenticate leaves every policy inert: full
-- visibility, no error, no log line, and a deployment that looks correct
-- because the migration ran and the policies exist. Silent failure in the
-- unsafe direction.
--
-- Measured on the development database on 24 August 2026: the application
-- connects as `marq`, which owns both tables. That is exactly the
-- configuration this guards against, and it is the default one.
ALTER TABLE deals FORCE ROW LEVEL SECURITY;
ALTER TABLE leads FORCE ROW LEVEL SECURITY;

-- Deals: own deals, plus deals carrying a commission split for this user.
CREATE POLICY deals_visibility ON deals
    FOR SELECT TO marq_agent_ro
    USING (
        app_requester_id() IS NOT NULL
        AND (
            agent_id = app_requester_id()
            OR app_holds_deal_split(deals.id)
        )
    );

-- Leads: the reporting subtree.
CREATE POLICY leads_visibility ON leads
    FOR SELECT TO marq_agent_ro
    USING (
        app_requester_id() IS NOT NULL
        AND agent_id IN (SELECT app_requester_subtree())
    );

COMMIT;

-- ============================================================
-- Rolling back
-- ============================================================
--
--   DROP POLICY IF EXISTS deals_visibility ON deals;
--   DROP POLICY IF EXISTS leads_visibility ON leads;
--   ALTER TABLE deals NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE leads NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE deals DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE leads DISABLE ROW LEVEL SECURITY;
--   DROP FUNCTION IF EXISTS app_holds_deal_split(bigint);
--   DROP FUNCTION IF EXISTS app_requester_subtree();
--   DROP FUNCTION IF EXISTS app_requester_id();
--
-- [claude] Policies before FORCE before DISABLE. Dropping in the other
-- order leaves a window where the table is readable without the policy,
-- which is the state this migration exists to prevent.
