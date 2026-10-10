from decimal import Decimal

from tests.grounding import numbers, ungrounded


def test_numbers_read_separators_and_decimals_but_not_identifiers() -> None:
    found = numbers("table_3 took 1,234.5 s over 60,000 calls ($1), 41.0% at 12 ms")

    assert found == {Decimal("1234.5"), Decimal("60000"), Decimal("41"), Decimal("12")}


def test_an_answer_quoting_tool_results_is_grounded() -> None:
    results = ['{"calls": 60000, "total_time": "3.2 min", "share_of_time": "41.5%"}']

    assert ungrounded("#3 ran 60,000 times for 3.2 min (41.5% of the time)", results) == set()


def test_a_number_no_tool_returned_is_ungrounded() -> None:
    results = ['{"calls": 60000, "mean_ms": 12.34}']

    assert ungrounded("60,000 calls at about 12.3 ms, 900 rows each", results) == {
        Decimal("12.3"),
        Decimal("900"),
    }


def test_small_integers_are_ranks_and_list_numbers() -> None:
    assert ungrounded("1. query #3 of the top 10", []) == set()
