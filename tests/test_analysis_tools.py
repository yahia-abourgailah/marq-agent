from app.tools.analysis import (
    calculate_average,
    calculate_difference,
    calculate_percentage,
    calculate_percentage_change,
    compare_periods,
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


def test_difference():
    result = calculate_difference.invoke({
        "first": 150,
        "second": 100,
    })

    assert result["difference"] == 50


def test_compare_periods():
    result = compare_periods.invoke({
        "current": {
            "deals": 150,
            "reservations": 40,
        },
        "previous": {
            "deals": 120,
            "reservations": 32,
        },
    })

    assert result["success"] is True
    assert result["comparison"]["deals"]["difference"] == 30
    assert result["comparison"]["deals"]["percentage_change"] == 25
