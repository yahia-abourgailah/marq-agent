import pytest

from app.sql.guard import ALLOWED_FUNCTIONS, SQLGuard, SQLGuardError

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
