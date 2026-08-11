from app.tools.deals import (
    calculate_average,
    calculate_percentage,
    calculate_percentage_change,
    calculate_pipeline_distribution,
    identify_stale_deals,
    rank_deals,
)


def test_percentage():
    result = calculate_percentage.invoke({
        "part": 25,
        "total": 100,
    })

    assert result["percentage"] == 25


def test_percentage_change():
    result = calculate_percentage_change.invoke({
        "old_value": 100,
        "new_value": 120,
    })

    assert result["percentage_change"] == 20


def test_average():
    result = calculate_average.invoke({
        "values": [10, 20, 30],
    })

    assert result["average"] == 20


def test_pipeline_distribution():
    result = calculate_pipeline_distribution.invoke({
        "counts": {
            "eoi": 40,
            "reservation": 30,
            "contracted": 30,
        },
    })

    assert result["total"] == 100
    assert result["distribution"]["eoi"] == 40


def test_identify_stale_deals():
    result = identify_stale_deals.invoke({
        "deals": [
            {"id": 1, "days_in_stage": 10},
            {"id": 2, "days_in_stage": 45},
            {"id": 3, "days_in_stage": 60},
        ],
        "threshold_days": 30,
    })

    assert result["stale_count"] == 2


def test_rank_deals():
    result = rank_deals.invoke({
        "deals": [
            {"id": 1, "area": 100},
            {"id": 2, "area": 300},
            {"id": 3, "area": 200},
        ],
        "field": "area",
        "descending": True,
        "limit": 2,
    })

    assert [deal["id"] for deal in result["deals"]] == [2, 3]