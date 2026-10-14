from datetime import datetime, timezone
import json
import logging
import os
import sys
from typing import Any

from src.core.update_context import get_update_context

_LOG_FORMAT = (
    "%(asctime)s [%(levelname)s]|[%(process)d]|[%(update_id)s:%(user_id)s]| "
    "%(name)s: %(message)s"
)
_PLAIN_FORMAT = "%(asctime)s [%(process)d]|[%(update_id)s:%(user_id)s]| %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

# Structured JSON logs for log aggregators that parse stdout (e.g. a
# container platform shipping to a log index); text formats stay the default
# for local/dev readability. Accepts the usual boolean spellings
# (1/true/t/yes/y/on, case-insensitive).
LOG_JSON = os.environ.get("LOG_JSON", "false").strip().lower() in {
    "1",
    "true",
    "t",
    "yes",
    "y",
    "on",
}


class UpdateContextFilter(logging.Filter):
    """Stamps every record with the update being handled and its sender.

    Outside an update (startup, shutdown, background work) there is no
    context, so records fall back to "-" rather than missing the fields:
    the formats reference them unconditionally.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = get_update_context()
        record.update_id = str(context.update_id) if context else "-"
        record.user_id = (
            str(context.user_id) if context and context.user_id is not None else "-"
        )
        return True


class JsonLogFormatter(logging.Formatter):
    """Renders one JSON object per line instead of the text formats."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "update_id": getattr(record, "update_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
            "process": record.process,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _build_formatter(fmt: str) -> logging.Formatter:
    if LOG_JSON:
        return JsonLogFormatter()
    return logging.Formatter(fmt, _DATE_FORMAT)


def _build_stream_handler(fmt: str) -> logging.StreamHandler:  # type: ignore[type-arg]
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.setFormatter(_build_formatter(fmt))
    handler.addFilter(UpdateContextFilter())
    return handler


def get_logger(name: str, *, plain_format: bool = False) -> logging.Logger:
    """Stdout-only logger. Containers own log shipping; files and rotation
    are deliberately not this process's job."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(log_level)
    logger.addHandler(
        _build_stream_handler(_PLAIN_FORMAT if plain_format else _LOG_FORMAT)
    )
    logger.propagate = False
    return logger


def setup_logging() -> None:
    """Give loggers that are not ours (aiogram, SQLAlchemy, asyncio) the same
    stdout handler, format and update stamp. Idempotent.

    Our own loggers keep their handler and do not propagate (get_logger), so
    nothing is printed twice.
    """
    root = logging.getLogger()
    if any(
        isinstance(f, UpdateContextFilter) for h in root.handlers for f in h.filters
    ):
        return
    root.setLevel(log_level)
    root.addHandler(_build_stream_handler(_LOG_FORMAT))
