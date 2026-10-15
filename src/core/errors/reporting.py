"""How a CoreException is logged and reported, wherever it is caught: the
update's error handler and background work report it the same way."""

from collections.abc import Mapping
import logging
from typing import Final

import sentry_sdk

from src.core.errors.exceptions import CoreException, LogLevel
from src.core.errors.scrubbed import reportable

LOG_LEVELS: Final[Mapping[LogLevel, int]] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def report_core_exception(logger: logging.Logger, error: CoreException) -> str | None:
    """Log `error` at its own level, with its traceback from ERROR up, and
    send it to Sentry when it asks for that.

    The line carries the class and `detail`, never `params`: they can hold
    what the user typed. Returns the Sentry event id, None when nothing was
    sent.
    """
    level = LOG_LEVELS[error.log_level]
    safe = reportable(error)
    logger.log(
        level,
        "%s: %s",
        type(error).__name__,
        error.detail or "-",
        exc_info=safe if level >= logging.ERROR else None,
    )
    if error.capture_to_sentry:
        return sentry_sdk.capture_exception(safe)
    return None
