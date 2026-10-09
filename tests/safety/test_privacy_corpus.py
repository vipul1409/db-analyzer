"""Row-data outputs pass the guard but never the privacy filter (agent profile)."""

import pytest

from db_analyzer.core.model import PrivacyRejected
from db_analyzer.safety import guard, privacy

from .corpus import ROW_DATA_OUTPUTS


@pytest.mark.parametrize("sql", ROW_DATA_OUTPUTS, ids=lambda s: s[:60])
def test_privacy_filter_rejects_row_data_the_guard_accepts(sql: str) -> None:
    guard.check(sql, "agent")

    with pytest.raises(PrivacyRejected):
        privacy.check_output(sql, frozenset())
