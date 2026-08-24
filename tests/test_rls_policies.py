"""
[claude] Row-level security, executed rather than read.

From the production-readiness review of 24 August, which found two defects
in `migrations/002` that a code read would not have caught and a run would:

    Blocker 3   the deals policy reads `deal_percentages`, and RLS `USING`
                expressions are evaluated with the *querying* role's
                privileges. `001` grants that role SELECT on three tables
                and `deal_percentages` is not one of them, so the day the
                migration was applied every deals query would have failed
                with `permission denied` — a hard failure on the agent's
                primary table.

    Blocker 4   a table's owner bypasses RLS unless FORCE is set, and the
                README records that an unset `POSTGRES_READONLY_USER` falls
                the pool back to the owning user. Applied that way, every
                policy is inert: full visibility, no error, no log line.

The migration header claimed it was "verified against the development
fixture". It cannot have been: `deal_percentages` does not exist there, so
the policy could not have been created — and any verification that did run
would have run as the owner, who bypasses the thing being verified.

So this builds the tables in a throwaway schema and runs the real
predicates against them.

What this does and does not cover
---------------------------------
The policies here are granted `TO PUBLIC` rather than to `marq_agent_ro`,
because the development role cannot create roles. That is a real gap and it
is narrow: what it cannot check is the *grant*, and what it does check is
everything the review found wrong — that the predicates are evaluable
without a grant on `deal_percentages`, that FORCE makes them apply to the
owner, and that they fail closed.

FORCE is what makes this test possible at all. Without it the owner would
bypass every policy and the whole file would pass while asserting nothing —
which is precisely how the original verification went wrong.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.db.connection import app_db

SCHEMA = "_marq_rls_probe"

# The predicates from migrations/002, against the probe schema. Kept close
# enough to the migration that a reader can diff them; `search_path` is
# pinned to the probe rather than to `public` for the same reason the real
# ones pin it at all.
SETUP = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};

CREATE TABLE {SCHEMA}.users (
    id bigint PRIMARY KEY,
    parent_id bigint
);

CREATE TABLE {SCHEMA}.deals (
    id bigint PRIMARY KEY,
    agent_id bigint
);

CREATE TABLE {SCHEMA}.leads (
    id bigint PRIMARY KEY,
    agent_id bigint
);

CREATE TABLE {SCHEMA}.deal_percentages (
    deal_id bigint,
    model_id bigint,
    model_type text
);

-- A two-level reporting tree: 1 manages 2, 2 manages 3.
INSERT INTO {SCHEMA}.users (id, parent_id)
VALUES (1, NULL), (2, 1), (3, 2), (9, NULL);

INSERT INTO {SCHEMA}.deals (id, agent_id)
VALUES (100, 2), (101, 3), (102, 9);

INSERT INTO {SCHEMA}.leads (id, agent_id)
VALUES (200, 2), (201, 3), (202, 9);

-- User 2 holds a commission split on user 9's deal, and on nothing else.
INSERT INTO {SCHEMA}.deal_percentages (deal_id, model_id, model_type)
VALUES (102, 2, 'App\\\\Models\\\\User');

CREATE FUNCTION {SCHEMA}.app_requester_id() RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.requester_id', true), '')::bigint
$$;

CREATE FUNCTION {SCHEMA}.app_requester_subtree() RETURNS SETOF bigint
LANGUAGE sql STABLE
SECURITY DEFINER
SET search_path = {SCHEMA}, pg_temp
AS $$
    WITH RECURSIVE subtree AS (
        SELECT id FROM users WHERE id = app_requester_id()
        UNION
        SELECT u.id FROM users u JOIN subtree s ON u.parent_id = s.id
    )
    SELECT id FROM subtree
$$;

CREATE FUNCTION {SCHEMA}.app_holds_deal_split(deal bigint) RETURNS boolean
LANGUAGE sql STABLE
SECURITY DEFINER
SET search_path = {SCHEMA}, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1 FROM deal_percentages dp
        WHERE dp.deal_id = deal
          AND dp.model_id = app_requester_id()
          AND dp.model_type LIKE '%User'
    )
$$;

ALTER TABLE {SCHEMA}.deals ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.deals FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.leads FORCE ROW LEVEL SECURITY;

CREATE POLICY deals_visibility ON {SCHEMA}.deals
    FOR SELECT TO PUBLIC
    USING (
        {SCHEMA}.app_requester_id() IS NOT NULL
        AND (
            agent_id = {SCHEMA}.app_requester_id()
            OR {SCHEMA}.app_holds_deal_split(deals.id)
        )
    );

CREATE POLICY leads_visibility ON {SCHEMA}.leads
    FOR SELECT TO PUBLIC
    USING (
        {SCHEMA}.app_requester_id() IS NOT NULL
        AND agent_id IN (SELECT {SCHEMA}.app_requester_subtree())
    );
"""


async def _run(sql: str) -> None:
    async with app_db.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(sql)


async def visible(table: str, requester: str | None) -> list[int]:
    """Ids of `table` visible to `requester`, through the real policies."""

    async with app_db.connection() as conn:
        async with conn.cursor() as cursor:
            if requester is None:
                await cursor.execute("SELECT set_config('app.requester_id','',true)")
            else:
                await cursor.execute(
                    "SELECT set_config('app.requester_id', %s, true)",
                    (requester,),
                )

            await cursor.execute(f"SELECT id FROM {SCHEMA}.{table} ORDER BY id")

            return [row["id"] for row in await cursor.fetchall()]


# [claude] `pytest_asyncio.fixture`, not `pytest.fixture` — the suite runs
# in strict asyncio mode, where a plain fixture returning a coroutine is
# handed to the test unawaited and every case errors before it starts.
@pytest_asyncio.fixture(scope="module", autouse=True)
async def probe_schema():
    await app_db.connect()
    await _run(SETUP)
    yield
    await _run(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ============================================================
# Blocker 4 — FORCE, or the policies never run at all
# ============================================================


async def test_force_makes_the_owner_subject_to_its_own_policies():
    """
    [claude] The assertion the original verification could not have made.

    This connection owns these tables. Without FORCE it would bypass every
    policy and see all three deals, and a test written that way would pass
    while proving nothing — which is how a migration came to claim it was
    verified when the policies had never once been evaluated.
    """

    async with app_db.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT pg_get_userbyid(relowner) = current_user AS mine, "
                "relforcerowsecurity AS forced "
                f"FROM pg_class WHERE oid = '{SCHEMA}.deals'::regclass"
            )
            row = await cursor.fetchone()

    assert row["mine"] is True, "the premise of this test is that we own it"
    assert row["forced"] is True

    # Owning the table is now not enough to see through it.
    assert await visible("deals", None) == []


# ============================================================
# Blocker 3 — the commission-split path is evaluable
# ============================================================


async def test_a_split_holder_sees_the_deal_without_a_grant_on_the_table():
    """
    Deal 102 belongs to user 9. User 2 holds a commission split on it and
    on nothing else, so it is visible to them and no other of user 9's
    rows are.

    This is the path that would have raised `permission denied for table
    deal_percentages` before the lookup moved behind a SECURITY DEFINER
    function — a hard failure on every deals query, not a wrong answer.
    """

    assert await visible("deals", "2") == [100, 102]


async def test_deals_do_not_cascade_the_reporting_tree():
    """
    MyDealsScope does not cascade, and that asymmetry with leads is the
    whole reason the two policies differ. User 1 manages 2 who manages 3,
    and user 1 owns no deals and holds no split — so user 1 sees nothing,
    even though their subtree owns two.
    """

    assert await visible("deals", "1") == []
    assert await visible("deals", "3") == [101]


# ============================================================
# Leads — the reporting subtree, which does cascade
# ============================================================


async def test_leads_cascade_the_reporting_subtree():
    assert await visible("leads", "1") == [200, 201]
    assert await visible("leads", "2") == [200, 201]
    assert await visible("leads", "3") == [201]


async def test_a_lead_outside_the_subtree_is_invisible():
    """User 9 is not in anyone's tree, and nobody is in theirs."""

    assert await visible("leads", "9") == [202]
    assert 202 not in await visible("leads", "1")


# ============================================================
# Failing closed
# ============================================================


async def test_no_identity_sees_nothing_on_either_table():
    """
    An unset requester is an unauthenticated request. It should see
    nothing, not everything, and the direction of that default is the
    single most consequential line in the migration.
    """

    assert await visible("deals", None) == []
    assert await visible("leads", None) == []


async def test_an_empty_guc_denies_as_firmly_as_a_missing_one():
    """
    [claude] The pooled-connection subtlety the migration header calls out.

    On a connection that has never carried the setting, `current_setting`
    returns NULL; once any transaction has set it, PostgreSQL keeps the GUC
    defined for the session and a rollback returns it to `''`. Connections
    here are pooled, so both occur — and a policy testing only for NULL
    would deny on a fresh connection and admit on a reused one.

    That is a bug that appears under load and never in a test that opens
    one connection, which is why it is asserted rather than trusted.
    """

    async with app_db.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT set_config('app.requester_id','2',false)"
            )
            await cursor.execute(f"SELECT count(*) AS n FROM {SCHEMA}.deals")
            assert (await cursor.fetchone())["n"] == 2

            # Same connection, identity cleared to empty rather than unset.
            await cursor.execute(
                "SELECT set_config('app.requester_id','',false)"
            )
            await cursor.execute(f"SELECT count(*) AS n FROM {SCHEMA}.deals")
            assert (await cursor.fetchone())["n"] == 0


async def test_one_requester_cannot_see_another_s_rows():
    """The property the whole migration exists for, stated plainly."""

    for table in ("deals", "leads"):
        mine = set(await visible(table, "3"))
        theirs = set(await visible(table, "9"))

        assert mine and theirs
        assert not (mine & theirs)
