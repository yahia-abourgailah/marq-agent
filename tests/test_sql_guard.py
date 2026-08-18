import pytest

from app.sql.guard import (
    ALLOWED_FUNCTIONS,
    MAX_ROWS,
    SQLGuard,
    SQLGuardError,
)

# [claude] One representative call per PostgreSQL-spelled function the guard is
# meant to permit. Keyed by the spelling the SQL agent writes, which is not
# always the name sqlglot ends up matching against ALLOWED_FUNCTIONS — that gap
# is the whole point of the self-test below.
ALLOWED_FUNCTION_PROBES = {
    "COUNT": "SELECT COUNT(*) FROM deals",
    "SUM": "SELECT SUM(lead_occurrence_count) FROM deals",
    "AVG": "SELECT AVG(lead_occurrence_count) FROM deals",
    "MIN": "SELECT MIN(created_at) FROM deals",
    "MAX": "SELECT MAX(created_at) FROM deals",
    "COALESCE": "SELECT COALESCE(area, '0') FROM deals",
    "NULLIF": "SELECT NULLIF(area, '') FROM deals",
    "DATE_TRUNC": "SELECT DATE_TRUNC('month', transaction_date) FROM deals",
    "DATE_PART": "SELECT DATE_PART('year', transaction_date) FROM deals",
    "EXTRACT": "SELECT EXTRACT(YEAR FROM transaction_date) FROM deals",
    "CURRENT_DATE": "SELECT id FROM deals WHERE batch_date < CURRENT_DATE",
    "CURRENT_TIMESTAMP": "SELECT id FROM deals WHERE created_at < CURRENT_TIMESTAMP",
    "ROUND": "SELECT ROUND(AVG(lead_occurrence_count), 2) FROM deals",
    "ABS": "SELECT ABS(lead_occurrence_count) FROM deals",
    "GREATEST": "SELECT GREATEST(created_at, updated_at) FROM deals",
    "LEAST": "SELECT LEAST(created_at, updated_at) FROM deals",
    "LOWER": "SELECT id FROM deals WHERE LOWER(client_name) = 'sara'",
    "UPPER": "SELECT id FROM deals WHERE UPPER(client_name) = 'SARA'",
    "TRIM": "SELECT TRIM(client_name) FROM deals",
    "LENGTH": "SELECT id FROM deals WHERE LENGTH(client_name) > 3",
}


def test_select_is_allowed():
    guard = SQLGuard()

    sql = guard.validate(
        "SELECT id, name FROM deals;"
    )

    assert "SELECT" in sql.upper()


def test_insert_is_rejected():
    guard = SQLGuard()

    with pytest.raises(SQLGuardError):
        guard.validate(
            "INSERT INTO deals (name) VALUES ('Test');"
        )


def test_update_is_rejected():
    guard = SQLGuard()

    with pytest.raises(SQLGuardError):
        guard.validate(
            "UPDATE deals SET name = 'Test';"
        )


def test_delete_is_rejected():
    guard = SQLGuard()

    with pytest.raises(SQLGuardError):
        guard.validate(
            "DELETE FROM deals;"
        )


def test_drop_is_rejected():
    guard = SQLGuard()

    with pytest.raises(SQLGuardError):
        guard.validate(
            "DROP TABLE deals;"
        )


def test_multiple_statements_are_rejected():
    guard = SQLGuard()

    with pytest.raises(SQLGuardError):
        guard.validate(
            "SELECT * FROM deals; DELETE FROM deals;"
        )


def test_data_modifying_cte_is_rejected():
    guard = SQLGuard()

    with pytest.raises(SQLGuardError):
        guard.validate(
            """
            WITH x AS (
                DELETE FROM deals
                RETURNING *
            )
            SELECT * FROM x;
            """
        )


# ============================================================
# [claude] Regression tests for the function allowlist.
# ============================================================


@pytest.mark.parametrize(
    "function_name",
    sorted(ALLOWED_FUNCTION_PROBES),
)
def test_every_allowed_function_is_actually_usable(function_name):
    """
    An entry in ALLOWED_FUNCTIONS must actually let the function through.

    sqlglot rewrites some functions while parsing, so the name the guard
    compares is its canonical name, not the PostgreSQL spelling. DATE_TRUNC
    was allowlisted and still rejected for exactly that reason — it parses to
    TIMESTAMP_TRUNC. This test fails on that class of drift instead of letting
    it sit silently until someone asks a time-series question.
    """

    guard = SQLGuard()

    guard.validate(ALLOWED_FUNCTION_PROBES[function_name])


def test_allowlist_and_probes_stay_in_sync():
    """Every PostgreSQL-spelled entry in the allowlist has a probe."""

    # Canonical-only names have no PostgreSQL spelling of their own; they
    # exist so sqlglot's rewrite of a probed function still matches.
    canonical_only = {"TIMESTAMP_TRUNC"}

    unprobed = ALLOWED_FUNCTIONS - set(ALLOWED_FUNCTION_PROBES) - canonical_only

    assert not unprobed, (
        f"These functions are allowlisted but never exercised: {sorted(unprobed)}. "
        "Add a probe to ALLOWED_FUNCTION_PROBES."
    )


# ============================================================
# [claude] SQL constructs the catalogue requires.
# ============================================================


def test_cast_is_allowed_for_varchar_area():
    """
    Catalogue rule 22 says `area` is varchar, and rule 41's worked example
    ranks deals by it. Both need a cast. CAST is a language construct that
    sqlglot models as exp.Func, so the guard used to reject it.
    """

    guard = SQLGuard()

    guard.validate(
        "SELECT id FROM deals "
        "WHERE deleted_at IS NULL "
        "ORDER BY CAST(area AS numeric) DESC "
        "LIMIT 5"
    )
    guard.validate("SELECT id FROM deals ORDER BY area::numeric DESC")


def test_case_expression_is_allowed():
    guard = SQLGuard()

    guard.validate(
        "SELECT CASE WHEN status = 'eoi' THEN 1 ELSE 0 END FROM deals"
    )


def test_date_trunc_is_allowed():
    """The regression that motivated the allowlist rewrite."""

    guard = SQLGuard()

    guard.validate(
        "SELECT DATE_TRUNC('month', transaction_date), COUNT(*) "
        "FROM deals WHERE deleted_at IS NULL GROUP BY 1"
    )


# ============================================================
# [claude] Widening CAST must not widen the data surface.
# ============================================================


@pytest.mark.parametrize(
    "query",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_sleep(300)",
        "SELECT lo_import('/etc/passwd')",
        # A blocked function hidden inside a now-permitted CAST.
        "SELECT CAST(pg_read_file('/etc/passwd') AS text)",
        # A blocked function hidden inside a now-permitted CASE.
        "SELECT CASE WHEN true THEN pg_sleep(300) END FROM deals",
    ],
)
def test_unapproved_functions_are_still_rejected(query):
    guard = SQLGuard()

    with pytest.raises(SQLGuardError):
        guard.validate(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM deal_percentages",
        "SELECT * FROM public.deals",
        "SELECT id FROM deals FOR UPDATE",
    ],
)
def test_out_of_catalogue_access_is_still_rejected(query):
    guard = SQLGuard()

    with pytest.raises(SQLGuardError):
        guard.validate(query)


def test_exists_is_allowed():
    """
    [claude] EXISTS is a subquery predicate, not a callable function.

    Blocking it broke the lead-to-deal conversion metric end to end: the SQL
    Agent wrote the correct query, the guard rejected it, and the Deals Agent
    fell back to dividing raw counts — a confidently wrong answer.
    """

    guard = SQLGuard()

    guard.validate(
        "SELECT count(*) FILTER (WHERE EXISTS ("
        "SELECT 1 FROM leads l WHERE l.id = deals.lead_id)) AS n FROM deals"
    )
    guard.validate(
        "SELECT count(*) AS n FROM deals d "
        "WHERE NOT EXISTS (SELECT 1 FROM leads l WHERE l.id = d.lead_id)"
    )


def test_exists_cannot_smuggle_a_blocked_table():
    """Widening EXISTS must not widen the data surface."""

    guard = SQLGuard(tables=frozenset({"leads", "users"}))

    with pytest.raises(SQLGuardError):
        guard.validate(
            "SELECT count(*) AS n FROM leads "
            "WHERE EXISTS (SELECT 1 FROM deals d WHERE d.lead_id = leads.id)"
        )

    with pytest.raises(SQLGuardError):
        guard.validate(
            "SELECT 1 WHERE EXISTS (SELECT 1 FROM pg_catalog.pg_authid)"
        )


# ============================================================
# [claude] Out-of-domain tables are a different failure.
# ============================================================


def test_a_table_outside_the_domain_raises_the_specific_error():
    """
    Subclassing keeps every existing `except SQLGuardError` working while
    letting the tool tell the two rejections apart.
    """

    from app.sql.guard import TableNotAllowedError

    guard = SQLGuard(tables=frozenset({"leads", "users"}))

    with pytest.raises(TableNotAllowedError):
        guard.validate("SELECT count(*) FROM deals")

    with pytest.raises(SQLGuardError):
        guard.validate("SELECT count(*) FROM deals")


def test_other_guard_rejections_are_not_table_errors():
    """
    [claude] The distinction that matters: these are worth a retry, a table
    rejection never is. Conflating them made the Leads Agent rephrase four
    times for a table its guard will never permit.
    """

    from app.sql.guard import TableNotAllowedError

    guard = SQLGuard(tables=frozenset({"leads"}))

    for query in (
        "SELECT pg_sleep(1) FROM leads",
        "DELETE FROM leads",
        "SELECT 1; SELECT 2",
    ):
        with pytest.raises(SQLGuardError) as exc:
            guard.validate(query)

        assert not isinstance(exc.value, TableNotAllowedError), query


# ============================================================
# [claude] Pressure tests — smuggling paths the earlier cases miss.
#
# Every one of these is a way to reach data through SQL that parses as a
# plain SELECT. The guard's job is to stay boring under all of them.
# ============================================================


@pytest.mark.parametrize(
    "query",
    [
        # Set operations — each arm needs checking, not just the first.
        "SELECT id FROM deals UNION SELECT id FROM pg_tables",
        "SELECT id FROM deals UNION ALL SELECT oid FROM pg_class",
        "SELECT id FROM deals INTERSECT SELECT id FROM information_schema.tables",
        "SELECT id FROM deals EXCEPT SELECT id FROM pg_stat_activity",
        # Subqueries in every clause position.
        "SELECT (SELECT count(*) FROM pg_tables) AS n FROM deals",
        "SELECT id FROM deals WHERE id IN (SELECT oid FROM pg_class)",
        "SELECT id FROM deals WHERE EXISTS (SELECT 1 FROM pg_shadow)",
        # Joins.
        "SELECT d.id FROM deals d JOIN pg_tables t ON true",
        "SELECT d.id FROM deals d, pg_class c",
        # CTEs that define a name and still read a real forbidden table.
        "WITH x AS (SELECT * FROM pg_tables) SELECT * FROM x",
        # Schema qualification.
        "SELECT * FROM public.deals",
        "SELECT * FROM pg_catalog.pg_tables",
        # Derived tables.
        "SELECT * FROM (SELECT * FROM pg_tables) AS sub",
    ],
)
def test_forbidden_tables_cannot_be_reached_through_any_clause(query):
    guard = SQLGuard(tables=frozenset({"deals", "leads", "users"}))

    with pytest.raises(SQLGuardError):
        guard.validate(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT pg_read_file('/etc/passwd') FROM deals",
        "SELECT pg_sleep(10) FROM deals",
        "SELECT lo_import('/etc/passwd') FROM deals",
        "SELECT query_to_xml('SELECT 1', true, true, '') FROM deals",
        "SELECT dblink('', 'SELECT 1') FROM deals",
        "SELECT current_setting('is_superuser') FROM deals",
        "SELECT set_config('x', 'y', true) FROM deals",
        "SELECT version() FROM deals",
        "SELECT pg_ls_dir('.') FROM deals",
    ],
)
def test_dangerous_functions_are_rejected(query):
    guard = SQLGuard(tables=frozenset({"deals"}))

    with pytest.raises(SQLGuardError):
        guard.validate(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM deals FOR UPDATE",
        "SELECT id FROM deals FOR SHARE",
        "SELECT id FROM deals FOR NO KEY UPDATE",
    ],
)
def test_row_locking_is_rejected(query):
    guard = SQLGuard(tables=frozenset({"deals"}))

    with pytest.raises(SQLGuardError):
        guard.validate(query)


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO deals (id) VALUES (1)",
        "UPDATE deals SET status = 'contracted'",
        "DELETE FROM deals",
        "TRUNCATE deals",
        "DROP TABLE deals",
        "ALTER TABLE deals ADD COLUMN x int",
        "CREATE TABLE x (id int)",
        "GRANT SELECT ON deals TO public",
        "COPY deals TO '/tmp/out.csv'",
        "CALL some_procedure()",
        "DO $$ BEGIN END $$",
    ],
)
def test_nothing_that_writes_or_commands_gets_through(query):
    guard = SQLGuard(tables=frozenset({"deals"}))

    with pytest.raises(SQLGuardError):
        guard.validate(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM deals; DROP TABLE deals",
        "SELECT id FROM deals;DELETE FROM leads",
        "SELECT id FROM deals; -- harmless\nUPDATE deals SET id = 1",
    ],
)
def test_statement_stacking_is_rejected(query):
    guard = SQLGuard(tables=frozenset({"deals", "leads"}))

    with pytest.raises(SQLGuardError):
        guard.validate(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM deals LIMIT 100000",
        # [claude] LIMIT ALL is PostgreSQL for "no limit", so it is the one
        # spelling that could quietly return the whole table. sqlglot drops
        # it during parsing and the guard then adds its own ceiling — the
        # right outcome, reached indirectly, which is exactly the kind of
        # thing worth pinning with a test.
        "SELECT id FROM deals LIMIT ALL",
        "SELECT id FROM deals",
        "SELECT id FROM deals OFFSET 100",
    ],
)
def test_every_query_comes_back_bounded(query):
    guard = SQLGuard(tables=frozenset({"deals"}))

    assert f"LIMIT {MAX_ROWS}" in guard.validate(query).upper()


@pytest.mark.parametrize("limit", ["LIMIT (SELECT 5)", "LIMIT -1"])
def test_limits_the_guard_cannot_evaluate_are_rejected(limit):
    """A ceiling that cannot be read cannot be enforced, so refuse instead."""

    guard = SQLGuard(tables=frozenset({"deals"}))

    with pytest.raises(SQLGuardError):
        guard.validate(f"SELECT id FROM deals {limit}")


def test_an_empty_or_whitespace_query_is_rejected():
    guard = SQLGuard(tables=frozenset({"deals"}))

    for query in ("", "   ", "\n\t"):
        with pytest.raises(SQLGuardError):
            guard.validate(query)


def test_the_domain_scope_is_per_guard_not_global():
    """
    Two guards in one process must not share a surface. This is the property
    that makes the leads agent genuinely unable to see deals.
    """

    deals_guard = SQLGuard(tables=frozenset({"deals", "leads", "users"}))
    leads_guard = SQLGuard(tables=frozenset({"leads", "users"}))

    query = "SELECT count(*) FROM deals"

    deals_guard.validate(query)

    with pytest.raises(SQLGuardError):
        leads_guard.validate(query)


# ============================================================
# [claude] Restricted columns — flagged in three consecutive reviews.
#
# These were listed in GENERIC_RULES and enforced nowhere: the guard passed
# `SELECT contract_price FROM deals` untouched, and nine columns across three
# agents rested on the model choosing to comply. The list now lives in the
# catalogue as data, and both the guard and the prompt text read it.
# ============================================================


@pytest.mark.parametrize(
    "query",
    [
        # The reviewer's own probes.
        "SELECT contract_price FROM deals",
        "SELECT SUM(down_payment) FROM deals",
        "SELECT unit_price AS p FROM deals",
        "SELECT id FROM deals WHERE contract_price > 100",
        "SELECT ad_spend_amount, cost_per_lead FROM leads",
        # Every other clause a column can hide in.
        "SELECT d.contract_price FROM deals d",
        "WITH x AS (SELECT contract_price FROM deals) SELECT * FROM x",
        "SELECT id FROM deals ORDER BY unit_price",
        "SELECT id FROM deals GROUP BY id HAVING SUM(down_payment) > 1",
        "SELECT id FROM deals WHERE id IN (SELECT id FROM deals WHERE unit_price > 5)",
        "SELECT count(*) FILTER (WHERE budget_amount > 0) FROM leads",
        "SELECT id FROM deals JOIN leads ON leads.budget_amount = deals.id",
        "SELECT CASE WHEN contract_price > 0 THEN 1 END FROM deals",
        # Aliasing *to* a restricted name leaks nothing, but the rule says
        # these names must not appear, and a column labelled contract_price
        # in a result is a reader's problem whatever the value behind it.
        "SELECT area AS contract_price FROM deals",
    ],
)
def test_restricted_columns_are_rejected_wherever_they_appear(query):
    from app.sql.guard import RestrictedColumnError

    guard = SQLGuard(tables=frozenset({"deals", "leads", "users"}))

    with pytest.raises(RestrictedColumnError):
        guard.validate(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT count(*) FROM deals WHERE deleted_at IS NULL",
        "SELECT area FROM deals",
        "SELECT id, status FROM deals JOIN leads ON deals.lead_id = leads.id",
        "SELECT avg(response_time_minutes) FROM leads",
    ],
)
def test_ordinary_queries_are_unaffected(query):
    """The column pass must not cost the queries the agent actually writes."""

    SQLGuard(tables=frozenset({"deals", "leads", "users"})).validate(query)


def test_the_guard_and_the_prompt_read_one_restricted_list():
    """
    The drift this prevents: a column added to the prompt prose and not to
    the guard is exactly the state all three reviews were flagging.
    """

    from app.sql.catalogue import GENERIC_RULES, RESTRICTED_COLUMNS
    from app.sql.guard import RestrictedColumnError

    guard = SQLGuard(tables=frozenset({"deals", "leads", "users"}))

    for column in RESTRICTED_COLUMNS:
        assert column in GENERIC_RULES, f"{column} missing from the prompt"

        with pytest.raises(RestrictedColumnError):
            guard.validate(f"SELECT {column} FROM deals")


def test_a_restricted_column_is_a_guard_error_too():
    """Existing `except SQLGuardError` handlers must keep working."""

    from app.sql.guard import RestrictedColumnError

    guard = SQLGuard(tables=frozenset({"deals"}))

    with pytest.raises(SQLGuardError):
        guard.validate("SELECT contract_price FROM deals")

    with pytest.raises(RestrictedColumnError):
        guard.validate("SELECT contract_price FROM deals")
