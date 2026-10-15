import json
import logging
import sys

import pytest

import loggers
from src.core.update_context import UpdateContext, bind_update_context


@pytest.fixture(autouse=True)
def _reset_json_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # LOG_JSON is read once at import time into a module-level flag; tests
    # that flip it monkeypatch the flag directly rather than re-importing.
    monkeypatch.setattr(loggers, "LOG_JSON", False)


def _make_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = []
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(loggers._build_stream_handler(loggers._LOG_FORMAT))
    return logger


def _last_record_via_capture(logger: logging.Logger) -> logging.LogRecord:
    # Relies on the stamping handler (and its UpdateContextFilter) running
    # before this capture handler: handlers run in the order they were added.
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    logger.addHandler(handler)
    try:
        logger.info("hello")
    finally:
        logger.removeHandler(handler)
    return captured[-1]


def test_update_filter_defaults_to_dashes_outside_an_update() -> None:
    logger = _make_test_logger("test.logger.no_context")
    record = _last_record_via_capture(logger)
    assert record.update_id == "-"
    assert record.user_id == "-"


def test_update_filter_reads_the_bound_context() -> None:
    logger = _make_test_logger("test.logger.with_context")
    context = UpdateContext(update_id=5, update_type="message", user_id=9)
    with bind_update_context(context):
        record = _last_record_via_capture(logger)
    assert record.update_id == "5"
    assert record.user_id == "9"


def test_update_filter_marks_a_missing_user_with_a_dash() -> None:
    logger = _make_test_logger("test.logger.no_user")
    with bind_update_context(UpdateContext(update_id=5, update_type="poll")):
        record = _last_record_via_capture(logger)
    assert record.update_id == "5"
    assert record.user_id == "-"


def test_get_logger_attaches_update_filter_to_its_handler() -> None:
    logger = loggers.get_logger("test.logger.get_logger_filter")
    handler = logger.handlers[0]
    assert any(isinstance(f, loggers.UpdateContextFilter) for f in handler.filters)


def test_json_formatter_round_trips_exact_field_set() -> None:
    logger = _make_test_logger("test.logger.json")
    with bind_update_context(
        UpdateContext(update_id=5, update_type="message", user_id=9)
    ):
        record = _last_record_via_capture(logger)

    formatter = loggers.JsonLogFormatter()
    formatted = formatter.format(record)
    payload = json.loads(formatted)

    assert set(payload) == {
        "timestamp",
        "level",
        "logger",
        "message",
        "update_id",
        "user_id",
        "process",
    }
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger.json"
    assert payload["message"] == "hello"
    assert payload["update_id"] == "5"
    assert payload["user_id"] == "9"
    assert isinstance(payload["process"], int)


def test_json_formatter_includes_exc_info_when_present() -> None:
    formatter = loggers.JsonLogFormatter()
    logger = logging.getLogger("test.logger.json_exc")
    try:
        raise ValueError("boom")
    except ValueError:
        record = logger.makeRecord(
            logger.name,
            logging.ERROR,
            __file__,
            0,
            "failure",
            (),
            exc_info=sys.exc_info(),
        )
    record.update_id = "-"
    record.user_id = "-"
    formatted = formatter.format(record)
    payload = json.loads(formatted)

    assert "exc_info" in payload
    assert "ValueError: boom" in payload["exc_info"]


def test_json_formatter_omits_exc_info_when_absent() -> None:
    formatter = loggers.JsonLogFormatter()
    logger = logging.getLogger("test.logger.json_no_exc")
    record = logger.makeRecord(
        logger.name, logging.INFO, __file__, 0, "fine", (), exc_info=None
    )
    record.update_id = "-"
    record.user_id = "-"
    formatted = formatter.format(record)
    payload = json.loads(formatted)

    assert "exc_info" not in payload


def test_get_logger_uses_json_formatter_when_log_json_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loggers, "LOG_JSON", True)
    handler = loggers._build_stream_handler(loggers._LOG_FORMAT)
    assert isinstance(handler.formatter, loggers.JsonLogFormatter)


def test_get_logger_uses_text_formatter_when_log_json_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loggers, "LOG_JSON", False)
    handler = loggers._build_stream_handler(loggers._LOG_FORMAT)
    assert isinstance(handler.formatter, logging.Formatter)
    assert not isinstance(handler.formatter, loggers.JsonLogFormatter)


def test_setup_logging_routes_third_party_loggers_through_one_handler() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    try:
        root.handlers.clear()
        loggers.setup_logging()
        # Idempotent: a second call must not add a second handler.
        loggers.setup_logging()
        assert len(root.handlers) == 1
        assert any(
            isinstance(f, loggers.UpdateContextFilter) for f in root.handlers[0].filters
        )

        # Added after the stamping handler, so it sees the stamped record.
        root.addHandler(_Capture())
        with bind_update_context(
            UpdateContext(update_id=5, update_type="message", user_id=9)
        ):
            logging.getLogger("third.party").warning("hello")
        assert [(r.update_id, r.user_id) for r in captured] == [("5", "9")]
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_task_arguments_stay_out_of_debug_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """taskiq's kicker and receiver log every task's arguments, and the LLM
    SDKs their request options with the prompt, at DEBUG; LOG_LEVEL=DEBUG
    must not print them."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_levels = {
        name: logging.getLogger(name).level for name in loggers.ARGUMENT_LOGGING_LOGGERS
    }
    seen: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    monkeypatch.setattr(loggers, "log_level", logging.DEBUG)
    try:
        root.handlers.clear()
        loggers.setup_logging()
        root.addHandler(_Capture())
        assert root.isEnabledFor(logging.DEBUG)
        for name in (
            "taskiq.kicker",
            "taskiq.receiver.receiver",
            "taskiq_redis.x",
            "anthropic._base_client",
            "openai._base_client",
            "httpx2",
        ):
            assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)
            assert logging.getLogger(name).isEnabledFor(logging.INFO)
            logging.getLogger(name).debug("Request options: my diary entry")
        assert seen == []
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
