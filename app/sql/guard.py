from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import expressions as exp


class SQLGuardError(ValueError):
    """Raised when generated SQL is not allowed to execute."""


@dataclass(frozen=True)
class SQLGuard:
    """Validate SQL before it reaches the database."""

    def validate(self, query: str) -> str:
        """Validate a read-only SQL statement and return normalized SQL."""

        if not query or not query.strip():
            raise SQLGuardError("SQL query cannot be empty.")

        try:
            statements = sqlglot.parse(query, read="postgres")
        except Exception as exc:
            raise SQLGuardError("Invalid SQL query.") from exc

        if len(statements) != 1:
            raise SQLGuardError("Only one SQL statement is allowed.")

        statement = statements[0]

        if statement is None:
            raise SQLGuardError("Invalid SQL query.")

        # Only allow SELECT-style statements.
        if not isinstance(
            statement,
            (exp.Select, exp.Union, exp.Intersect, exp.Except),
        ):
            raise SQLGuardError("Only SELECT queries are allowed.")

        # Reject any data-modifying operations inside the query.
        if any(isinstance(node, exp.DML) for node in statement.walk()):
            raise SQLGuardError("Data-modifying SQL is not allowed.")

        # Reject DDL such as CREATE, ALTER, DROP, etc.
        if any(isinstance(node, exp.DDL) for node in statement.walk()):
            raise SQLGuardError("DDL statements are not allowed.")

        # Reject SQL commands.
        if any(isinstance(node, exp.Command) for node in statement.walk()):
            raise SQLGuardError("SQL commands are not allowed.")

        return statement.sql(dialect="postgres")


__all__ = ["SQLGuard", "SQLGuardError"]