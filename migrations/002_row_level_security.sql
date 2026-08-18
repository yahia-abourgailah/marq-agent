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

BEGIN;

CREATE OR REPLACE FUNCTION app_requester_id() RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.requester_id', true), '')::bigint
$$;

-- The reporting subtree, for leads. Anchored on the requester and walking
-- users.parent_id downwards.
CREATE OR REPLACE FUNCTION app_requester_subtree() RETURNS SETOF bigint
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE subtree AS (
        SELECT id FROM users WHERE id = app_requester_id()
        UNION
        SELECT u.id FROM users u JOIN subtree s ON u.parent_id = s.id
    )
    SELECT id FROM subtree
$$;

ALTER TABLE deals ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;

-- Deals: own deals, plus deals carrying a commission split for this user.
CREATE POLICY deals_visibility ON deals
    FOR SELECT TO marq_agent_ro
    USING (
        app_requester_id() IS NOT NULL
        AND (
            agent_id = app_requester_id()
            OR EXISTS (
                SELECT 1 FROM deal_percentages dp
                WHERE dp.deal_id = deals.id
                  AND dp.model_id = app_requester_id()
                  AND dp.model_type LIKE '%User'
            )
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
--   ALTER TABLE deals DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE leads DISABLE ROW LEVEL SECURITY;
--   DROP POLICY IF EXISTS deals_visibility ON deals;
--   DROP POLICY IF EXISTS leads_visibility ON leads;
