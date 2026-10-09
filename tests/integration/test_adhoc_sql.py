"""Ad-hoc SQL the LLM writes, through the facade: agent guard profile plus privacy filter."""

import pytest

from db_analyzer.core.model import PrivacyRejected
from db_analyzer.service import AnalyzerService

from .conftest import SUPPORTED, seeded_dsn

pytestmark = pytest.mark.integration


@pytest.fixture(params=SUPPORTED)
def connection_id(
    request: pytest.FixtureRequest, service: AnalyzerService, monkeypatch: pytest.MonkeyPatch
) -> str:
    monkeypatch.setenv("DBX_TEST_SHOP_DSN", seeded_dsn(request.param))
    return service.add_connection("shop", dsn_env="DBX_TEST_SHOP_DSN").id


def test_select_email_is_rejected_before_execution_and_audited(
    service: AnalyzerService, connection_id: str
) -> None:
    sql = "SELECT email FROM accounts LIMIT 5"

    with pytest.raises(PrivacyRejected):
        service.run_sql(connection_id, sql, purpose="list emails")

    [entry] = service.audit(connection_id)
    assert (entry.sql, entry.decision, entry.purpose) == (sql, "rejected", "list emails")


def test_aggregate_only_sql_returns_its_rows(service: AnalyzerService, connection_id: str) -> None:
    rows = service.run_sql(
        connection_id,
        "SELECT count(*) FILTER (WHERE status = 'pending') AS pending, count(*) AS total "
        "FROM bookings",
        purpose="pending bookings",
    )

    assert rows == [{"pending": 2_000, "total": 20_000}]
    assert [e.decision for e in service.audit(connection_id)] == ["executed"]
