import pytest

from db_analyzer.adapters.postgres import plans
from db_analyzer.safety import guard


def test_a_select_is_planned_as_it_is() -> None:
    sql = "SELECT id FROM bookings WHERE account_id = $1"

    assert plans.plannable(sql) == plans.Plannable(sql, row_lookup_only=False)


def test_an_update_is_planned_as_the_select_that_finds_its_rows() -> None:
    p = plans.plannable("UPDATE bookings SET amount = amount WHERE tenant_id = $1 AND status = $2")

    assert isinstance(p, plans.Plannable) and p.row_lookup_only
    assert p.sql == ("SELECT amount = amount FROM bookings WHERE tenant_id = $1 AND status = $2")
    guard.check(p.sql, "agent")  # a plain SELECT: no profile has to accept DML


def test_set_values_stay_in_the_select_so_their_parameters_keep_a_type() -> None:
    p = plans.plannable("UPDATE bookings SET status = $1 WHERE id = $2")

    assert isinstance(p, plans.Plannable)
    assert p.sql == "SELECT status = $1 FROM bookings WHERE id = $2"


def test_update_from_keeps_its_from_items() -> None:
    p = plans.plannable(
        "UPDATE bookings b SET amount = 0 FROM accounts a WHERE a.id = b.account_id AND a.id = $1"
    )

    assert isinstance(p, plans.Plannable)
    assert p.sql == (
        "SELECT amount = 0 FROM bookings AS b, accounts AS a "
        "WHERE a.id = b.account_id AND a.id = $1"
    )


def test_a_delete_keeps_its_using_items_and_with_clause() -> None:
    p = plans.plannable(
        "WITH old AS (SELECT id FROM accounts WHERE created_at < $1) "
        "DELETE FROM bookings USING old WHERE bookings.account_id = old.id"
    )

    assert isinstance(p, plans.Plannable) and p.row_lookup_only
    assert p.sql == (
        "WITH old AS (SELECT id FROM accounts WHERE created_at < $1) "
        "SELECT 1 FROM bookings, old WHERE bookings.account_id = old.id"
    )


def test_insert_select_is_planned_as_its_select() -> None:
    p = plans.plannable("INSERT INTO archive SELECT * FROM bookings WHERE starts_at < $1")

    assert isinstance(p, plans.Plannable) and p.row_lookup_only
    assert p.sql == "SELECT * FROM bookings WHERE starts_at < $1"


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("INSERT INTO bookings (id) VALUES ($1)", "INSERT … VALUES looks up no rows"),
        (
            "MERGE INTO bookings b USING accounts a ON a.id = b.account_id "
            "WHEN MATCHED THEN DELETE",
            "MERGE is not planned",
        ),
    ],
)
def test_statements_without_a_row_lookup_to_plan_say_why(sql: str, reason: str) -> None:
    assert plans.plannable(sql) == reason
