"""
Query-safety validation for AI-generated SQL.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import expressions as exp

from app.sql.catalogue import get_catalogue


class SQLGuardError(ValueError):
    """Raised when generated SQL is not safe to execute."""


MAX_ROWS = 500


# These functions are useful for normal CRM analytics.
#
# This is a query-safety allowlist.
# It is NOT an authorization mechanism.
#
# PostgreSQL permissions and RLS remain the authoritative
# database security controls.
#
# [claude] IMPORTANT — namespace. These are matched against sqlglot's
# *canonical* function names (`exp.Func.sql_name()`), which are not always the
# PostgreSQL spelling the model writes. sqlglot rewrites some functions when it
# parses them, so an entry spelled the PostgreSQL way can silently never match.
#
# Two entries in the original set were dead for exactly that reason:
#
#     DATE_TRUNC(...)  parses to TimestampTrunc  -> sql_name "TIMESTAMP_TRUNC"
#     DATE_PART(...)   parses to Extract         -> sql_name "EXTRACT"
#
# So DATE_TRUNC — the single most useful function for any time-series CRM
# question — was allowlisted and still rejected. DATE_PART worked only by
# accident, because it collapses into the allowlisted EXTRACT.
#
# Both spellings are kept below so the set stays readable and survives a
# future sqlglot release that changes canonicalisation either way. Extra
# names are harmless: they only ever widen this set by the same function
# under a different label.
#
# `tests/test_sql_guard.py::test_every_allowed_function_is_actually_usable`
# parses one representative call per PostgreSQL-spelled function and asserts
# the guard accepts it. Add a probe there whenever you add a function here,
# otherwise this drift returns silently.
ALLOWED_FUNCTIONS = {
    # Aggregates
    "COUNT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
    # Null handling
    "COALESCE",
    "NULLIF",
    # Dates
    "DATE_TRUNC",        # [claude] PostgreSQL spelling
    "TIMESTAMP_TRUNC",   # [claude] what sqlglot actually produces for it
    "DATE_PART",         # [claude] PostgreSQL spelling
    "EXTRACT",           # [claude] what sqlglot produces for DATE_PART and EXTRACT
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",  # [claude] NOW() also canonicalises to this
    # [claude] Numeric helpers. ROUND is needed for any percentage answer;
    # catalogue rule 22 requires numeric work on `area`, which is varchar.
    "ROUND",
    "ABS",
    "GREATEST",
    "LEAST",
    # [claude] Text helpers. Needed for name lookups such as catalogue rule 13
    # ("How many deals does <person> own?" -> filter on users.name).
    "LOWER",
    "UPPER",
    "TRIM",
    "LENGTH",
}


@dataclass(frozen=True)
class SQLGuard:
    """
    Validate AI-generated SQL before it reaches PostgreSQL.

    [claude] `tables` scopes the allowlist.

    The guard used to call get_catalogue() itself, so every guard in the
    process permitted the same tables. That is fine with one agent and wrong
    with several: a Leads Agent and a Deals Agent should not share a query
    surface, and a guard that cannot be narrowed cannot express that.

    Passing None keeps the full catalogue, so existing callers are
    unaffected.

    Responsibilities
    -----------------
    The Guard is a defense-in-depth query-safety layer.

    It ensures that generated SQL:

    - is syntactically valid PostgreSQL
    - contains only one statement
    - stays within the SQL agent's catalogue
    - does not use dangerous/unapproved functions
    - does not use row locking
    - does not access arbitrary schemas
    - has a bounded result size

    Security boundaries
    -------------------
    The Guard is NOT the authoritative database security layer.

    PostgreSQL permissions enforce database-level permissions.

    The PostgreSQL read-only role (`marq_agent_ro`) prevents the
    application from modifying CRM data.

    PostgreSQL Row-Level Security (RLS) determines which rows the
    authenticated user is actually allowed to access.

    Therefore:

        Guard  -> query safety
        PostgreSQL role -> database permissions
        RLS -> row-level authorization
    """

    # [claude] Table names this guard permits. None means the whole
    # catalogue.
    tables: frozenset[str] | None = None

    def allowed_tables(self) -> set[str]:
        """
        Resolve the permitted table names, lowercased.

        Deriving the default from the catalogue keeps the schema the agent
        is shown and the surface the guard permits from drifting apart.
        """

        if self.tables is not None:
            return {name.lower() for name in self.tables}

        catalogue_tables = get_catalogue().get("tables", {})

        if not isinstance(catalogue_tables, dict):
            raise SQLGuardError("Invalid SQL catalogue.")

        return {str(name).lower() for name in catalogue_tables}

    def validate(self, query: str) -> str:
        """
        Validate and normalize one AI-generated SQL query.

        The returned SQL is safe to pass to the database executor,
        subject to PostgreSQL permissions and RLS.
        """

        if not query or not query.strip():
            raise SQLGuardError(
                "SQL query cannot be empty."
            )

        # ---------------------------------------------------------
        # Parse SQL
        # ---------------------------------------------------------

        try:
            statements = sqlglot.parse(
                query,
                read="postgres",
            )
        except Exception as exc:
            raise SQLGuardError(
                "Invalid SQL query."
            ) from exc

        statements = [
            statement
            for statement in statements
            if statement is not None
        ]

        # ---------------------------------------------------------
        # One statement only
        # ---------------------------------------------------------

        if len(statements) != 1:
            raise SQLGuardError(
                "Only one SQL statement is allowed."
            )

        statement = statements[0]

        # ---------------------------------------------------------
        # Fail-fast read-only check
        # ---------------------------------------------------------
        #
        # PostgreSQL is the authoritative enforcement point because
        # the application connects using the marq_agent_ro role.
        #
        # This check exists only as defense-in-depth and to reject
        # obviously invalid AI output before sending it to PostgreSQL.
        # ---------------------------------------------------------

        if not isinstance(
            statement,
            (
                exp.Select,
                exp.Union,
                exp.Intersect,
                exp.Except,
            ),
        ):
            raise SQLGuardError(
                "Only read queries are allowed."
            )

        if any(
            isinstance(node, exp.DML)
            for node in statement.walk()
        ):
            raise SQLGuardError(
                "Data-modifying SQL is not allowed."
            )

        if any(
            isinstance(node, exp.DDL)
            for node in statement.walk()
        ):
            raise SQLGuardError(
                "DDL statements are not allowed."
            )

        if any(
            isinstance(node, exp.Command)
            for node in statement.walk()
        ):
            raise SQLGuardError(
                "SQL commands are not allowed."
            )

        # ---------------------------------------------------------
        # Row locking
        # ---------------------------------------------------------
        #
        # A SELECT ... FOR UPDATE is still technically a SELECT,
        # but it requests database locks.
        #
        # The AI agent has no reason to perform row locking.
        # ---------------------------------------------------------

        lock_expression = getattr(
            exp,
            "Lock",
            None,
        )

        if lock_expression is not None:
            if any(
                isinstance(node, lock_expression)
                for node in statement.walk()
            ):
                raise SQLGuardError(
                    "Row-locking queries are not allowed."
                )

        # ---------------------------------------------------------
        # Table allowlist
        # ---------------------------------------------------------
        #
        # This controls the SQL agent's query surface.
        #
        # It is NOT user authorization.
        #
        # RLS still decides which rows the current database session
        # is allowed to see.
        # ---------------------------------------------------------

        allowed_tables = self.allowed_tables()

        # CTE names are temporary query-local names and therefore
        # are not real database tables.
        cte_names = {
            str(cte.alias_or_name).lower()
            for cte in statement.find_all(exp.CTE)
            if cte.alias_or_name
        }

        for table in statement.find_all(exp.Table):

            table_name = table.name.lower()

            # Allow references to CTEs.
            if table_name in cte_names:
                continue

            if table_name not in allowed_tables:
                raise SQLGuardError(
                    f"Table '{table.name}' is not allowed."
                )

            # Do not allow schema-qualified or database-qualified
            # table references such as:
            #
            # pg_catalog.pg_tables
            # public.deals
            # other_schema.deals
            #
            # The SQL agent must stay within the catalogue.
            if table.db or table.catalog:
                raise SQLGuardError(
                    f"Qualified table "
                    f"'{table.sql(dialect='postgres')}' "
                    "is not allowed."
                )

        # ---------------------------------------------------------
        # Function allowlist
        # ---------------------------------------------------------
        #
        # This prevents the AI from generating arbitrary PostgreSQL
        # functions such as:
        #
        #   pg_read_file(...)
        #   pg_sleep(...)
        #   lo_import(...)
        #
        # PostgreSQL permissions remain the actual database
        # security boundary.
        # ---------------------------------------------------------

        for node in statement.walk():

            function_name = _function_name(node)

            if function_name is None:
                continue

            if function_name not in ALLOWED_FUNCTIONS:
                raise SQLGuardError(
                    f"Function '{function_name}' "
                    "is not allowed."
                )

        # ---------------------------------------------------------
        # Result-size protection
        # ---------------------------------------------------------
        #
        # Prevent the SQL agent from returning an unbounded number
        # of rows to the application/model.
        #
        # This is a resource-protection mechanism, not authorization.
        # ---------------------------------------------------------

        limit = statement.args.get("limit")

        if limit is None:

            statement = statement.limit(
                MAX_ROWS
            )

        else:

            limit_value = _extract_limit_value(
                limit
            )

            if limit_value is None:
                raise SQLGuardError(
                    "LIMIT must be a numeric constant."
                )

            if limit_value > MAX_ROWS:

                statement = statement.limit(
                    MAX_ROWS
                )

        # ---------------------------------------------------------
        # Normalize SQL
        # ---------------------------------------------------------

        return statement.sql(
            dialect="postgres"
        )


def _function_name(
    node: exp.Expression,
) -> str | None:
    """
    Return the SQL function name when the expression represents
    an actual function call.

    SQL operators such as AND, OR, comparisons, predicates,
    and arithmetic expressions are not functions.
    """

    if not isinstance(node, exp.Func):
        return None

    # sqlglot versions can represent some SQL operators through
    # classes that overlap with exp.Func.
    #
    # Use class names instead of referencing optional expression
    # classes that may not exist in the installed version.
    non_function_names = {
        # [claude] SQL language constructs, not callable functions. sqlglot
        # models them as exp.Func subclasses, so without this they were
        # rejected as "Function 'CAST' is not allowed" / "'CASE' is not
        # allowed" — blocking SQL the catalogue itself demands:
        #
        #   rule 22  `area` is varchar, so ORDER BY needs CAST(area AS numeric)
        #   rule 41  worked example "Top 5 deals by area" needs exactly that
        #
        # These widen the *expression* surface, not the data surface: the
        # table allowlist still bars pg_catalog, and casting an allowlisted
        # column reaches no data the query could not already read.
        #
        # CASE parses into two node types, so both are listed.
        #
        # EXISTS is a subquery predicate, not a callable function. Blocking
        # it broke the lead-to-deal conversion metric: the SQL Agent wrote
        # the correct `COUNT(*) FILTER (WHERE EXISTS (...))`, the guard
        # rejected it, and the Deals Agent fell back to dividing two raw
        # counts — reporting 35.63% where the real figure is 31.67%. A guard
        # rejection that pushes an agent onto a wrong answer is worse than
        # the query it blocked. The tables inside the subquery are still
        # checked against the allowlist.
        "cast",
        "case",
        "if",
        "exists",
        "and",
        "or",
        "not",
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "like",
        "ilike",
        "between",
        "in",
        "is",
        "is_null",
        "is_not_null",
        "add",
        "sub",
        "mul",
        "div",
        "mod",
        "neg",
    }

    if node.__class__.__name__.lower() in non_function_names:
        return None

    if isinstance(node, exp.Anonymous):
        return node.name.upper()

    try:
        return node.sql_name().upper()
    except Exception:
        return None


def _extract_limit_value(
    limit: exp.Expression,
) -> int | None:
    """
    Extract a numeric LIMIT value.

    Dynamic LIMIT expressions are rejected so that the Guard can
    enforce the maximum result size deterministically.
    """

    expression = limit.args.get(
        "expression"
    )

    if (
        isinstance(expression, exp.Literal)
        and expression.is_int
    ):
        try:
            value = int(
                expression.this
            )
        except (
            TypeError,
            ValueError,
        ):
            return None

        if value < 0:
            return None

        return value

    return None


__all__ = [
    # [claude] ALLOWED_FUNCTIONS was missing; the guard tests import it.
    "ALLOWED_FUNCTIONS",
    "MAX_ROWS",
    "SQLGuard",
    "SQLGuardError",
]
