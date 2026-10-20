"""The log: events and errors only. No message text and no tool arguments are ever passed to it, and a filter
redacts anything secret-looking as a second line of defence."""

from __future__ import annotations

import logging
from pathlib import Path

from .permissions.redact import redact

LOGGER = "custom_console"
MAX_BYTES = 1_000_000
BACKUPS = 3


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(path: Path | None, level: str = "info", *, debug: bool = False) -> logging.Logger:
    """Log to a rotating file (nothing when `path` is None). `debug` adds request and response metadata."""
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger(LOGGER)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    if path is None:
        logger.addHandler(logging.NullHandler())
        return logger
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger
