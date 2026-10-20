"""Opt-in file logging for debugging. Off unless a file is named, so normal runs stay quiet.

Nothing is configured on import: the entry points (`dbx serve`, `analyze`, `chat`) call
`setup_logging()` once, which writes the `db_analyzer` logger tree to the file the operator chose
via `DBX_LOG_FILE` (level from `DBX_LOG_LEVEL`, default INFO). Modules log with the standard
`logging.getLogger(__name__)`; those names sit under `db_analyzer`, so they inherit this setup.
"""

import logging
import os
from pathlib import Path

LOGGER_NAME = "db_analyzer"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(path: str | Path | None = None, level: str | None = None) -> Path | None:
    """Send `db_analyzer` logs to `path` (or $DBX_LOG_FILE) at `level` (or $DBX_LOG_LEVEL, default
    INFO), and return the file. A no-op returning None when no file is named. Safe to call more
    than once: it replaces the handler it added rather than stacking a new one each time."""
    target = path or os.environ.get("DBX_LOG_FILE")
    if not target:
        return None
    level_name = (level or os.environ.get("DBX_LOG_LEVEL") or "INFO").upper()
    file = Path(target).expanduser()
    file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level_name)
    for existing in list(logger.handlers):
        if getattr(existing, "_db_analyzer", False):
            logger.removeHandler(existing)
            existing.close()
    handler = logging.FileHandler(file, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler._db_analyzer = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.info("logging started: file=%s level=%s", file, level_name)
    return file
