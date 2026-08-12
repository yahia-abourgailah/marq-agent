from app.sql.catalogue import get_catalogue


def test_deals_catalogue():
    catalogue = get_catalogue()

    assert "deals" in catalogue["tables"]

    columns = {
        column["name"]
        for column in catalogue["tables"]["deals"]["columns"]
    }

    assert "id" in columns
    assert "status" in columns
    assert "agent_id" in columns
    assert "project_id" in columns
    assert "transaction_date" in columns

    # Sensitive monetary fields must not be exposed.
    assert "unit_price" not in columns
    assert "contract_price" not in columns
    assert "reservation_price" not in columns
    assert "down_payment" not in columns
