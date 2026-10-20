import logging
from pathlib import Path

from db_analyzer.logconfig import LOGGER_NAME, setup_logging


def _reset() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        if getattr(h, "_db_analyzer", False):
            logger.removeHandler(h)
            h.close()


def test_no_file_named_is_a_no_op(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DBX_LOG_FILE", raising=False)
    try:
        assert setup_logging() is None
        handlers = logging.getLogger(LOGGER_NAME).handlers
        assert not any(getattr(h, "_db_analyzer", False) for h in handlers)
    finally:
        _reset()


def test_logs_from_a_child_logger_reach_the_file(tmp_path: Path) -> None:
    target = tmp_path / "logs" / "dbx.log"  # a missing parent is created
    try:
        assert setup_logging(target) == target
        logging.getLogger("db_analyzer.adapters.postgres.workload").info("window=%s", "14 days")
        assert "window=14 days" in target.read_text()
    finally:
        _reset()


def test_calling_twice_does_not_stack_handlers(tmp_path: Path) -> None:
    try:
        setup_logging(tmp_path / "a.log")
        setup_logging(tmp_path / "b.log")
        handlers = logging.getLogger(LOGGER_NAME).handlers
        ours = [h for h in handlers if getattr(h, "_db_analyzer", False)]
        assert len(ours) == 1
    finally:
        _reset()
