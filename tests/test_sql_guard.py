import pytest

from app.sql.guard import SQLGuard, SQLGuardError


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