"""Suite (a): QueryGuard rejects every corpus statement under both profiles. Runs with the
unit tests, so it blocks merge without a database."""

import pytest

from db_analyzer.core.model import QueryRejected
from db_analyzer.safety import guard

from .corpus import CORPUS, Forbidden


def test_corpus_is_large_and_unique() -> None:
    assert len(CORPUS) >= 200
    assert len({f.sql for f in CORPUS}) == len(CORPUS)


@pytest.mark.parametrize("profile", ["agent", "internal"])
@pytest.mark.parametrize("forbidden", CORPUS, ids=lambda f: f.sql[:60])
def test_guard_rejects_corpus_statement(forbidden: Forbidden, profile: guard.Profile) -> None:
    with pytest.raises(QueryRejected):
        guard.check(forbidden.sql, profile)
