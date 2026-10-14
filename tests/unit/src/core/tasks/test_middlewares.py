import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import sentry_sdk
from sqlalchemy.exc import DBAPIError
from taskiq import InMemoryBroker, TaskiqMessage, TaskiqResult
from taskiq.middlewares import SmartRetryMiddleware

from src.core.errors.scrubbed import ScrubbedError
from src.core.tasks import middlewares as middlewares_module
from src.core.tasks.middlewares import (
    FAILURE_REPORTER_STATE,
    SENTRY_EVENT_ID_LABEL,
    SentryMiddleware,
    TaskFailureAlertMiddleware,
)
from src.core.telegram.alerts import Alert
from tests.helpers.logs import captured_logs


def _message() -> TaskiqMessage:
    return TaskiqMessage(
        task_id="task-1",
        task_name="user.notify_admins",
        labels={},
        labels_types={},
        args=[],
        kwargs={},
    )


def _failed() -> TaskiqResult[Any]:
    return TaskiqResult(
        is_err=True,
        log=None,
        return_value=None,
        execution_time=0.01,
        labels={},
    )


def test_on_error_reports_exception_with_task_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker has no request to group events by; without the task name and
    id an event cannot be traced back to the task that failed."""
    captured: dict[str, Any] = {}

    def fake_capture_exception(exception: BaseException) -> None:
        scope = sentry_sdk.get_current_scope()
        captured["exception"] = exception
        captured["tag"] = scope._tags.get("taskiq.task_name")
        captured["context"] = scope._contexts.get("taskiq")

    monkeypatch.setattr(sentry_sdk, "capture_exception", fake_capture_exception)
    exception = RuntimeError("boom")

    SentryMiddleware().on_error(_message(), _failed(), exception)

    assert captured["exception"] is exception
    assert captured["tag"] == "user.notify_admins"
    assert captured["context"] == {"task_id": "task-1"}


def test_a_driver_error_reaches_sentry_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """taskiq itself can raise past the task's own scrubbing (parameter
    validation, DI); PostgreSQL's DETAIL names the row it refused."""
    captured: list[BaseException] = []
    monkeypatch.setattr(sentry_sdk, "capture_exception", captured.append)
    error = DBAPIError("UPDATE", {}, Exception("DETAIL: Key (id)=(4242)"))

    SentryMiddleware().on_error(_message(), _failed(), error)

    [reported] = captured
    assert isinstance(reported, ScrubbedError)
    assert "4242" not in str(reported)


def test_the_sentry_event_id_is_left_for_the_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sentry_sdk, "capture_exception", lambda error: "evt-1")
    result = _failed()

    SentryMiddleware().on_error(_message(), result, RuntimeError("boom"))

    assert result.labels[SENTRY_EVENT_ID_LABEL] == "evt-1"


def test_a_cancelled_task_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that stops cancels the tasks it runs; every deploy would
    otherwise file an event."""
    captured: list[BaseException] = []
    monkeypatch.setattr(sentry_sdk, "capture_exception", captured.append)

    SentryMiddleware().on_error(_message(), _failed(), asyncio.CancelledError())

    assert captured == []


class RecordingReporter:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    async def alert_error(self, alert: Alert) -> None:
        self.alerts.append(alert)


class Attempts:
    def __init__(self) -> None:
        self.count = 0


def _failing_task(
    attempts: Attempts, error: BaseException | None = None
) -> Callable[[], Awaitable[None]]:
    # taskiq renames the function it registers, which a bound method refuses.
    async def fail() -> None:
        attempts.count += 1
        raise error or RuntimeError("task failed")

    return fail


async def _run_failing(
    *, retry: bool, error: BaseException | None = None
) -> tuple[RecordingReporter, Attempts]:
    broker = InMemoryBroker(await_inplace=True)
    broker.add_middlewares(
        TaskFailureAlertMiddleware(),
        SmartRetryMiddleware(
            default_retry_count=3, default_delay=0, schedule_source=None
        ),
        SentryMiddleware(),
    )
    reporter = RecordingReporter()
    broker.state[FAILURE_REPORTER_STATE] = reporter
    attempts = Attempts()
    labels = {"retry_on_error": True} if retry else {}
    broker.register_task(
        _failing_task(attempts, error), task_name="test.fail", **labels
    )
    await broker.startup()
    try:
        await broker.find_task("test.fail").kiq()
    finally:
        await broker.shutdown()
    return reporter, attempts


async def test_a_retried_task_alerts_once_on_the_final_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every attempt reaches Sentry; developers hear about the task once,
    when it has given up."""
    monkeypatch.setattr(
        middlewares_module.sentry_sdk, "capture_exception", lambda e: "evt"
    )
    reporter, attempts = await _run_failing(retry=True)
    assert attempts.count == 3
    [alert] = reporter.alerts
    assert alert.origin == "task test.fail"
    assert alert.sentry_event_id == "evt"
    # The id is what keeps a later redelivery of the same task from alerting.
    assert alert.task_id is not None


async def test_a_task_without_retries_alerts_on_its_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        middlewares_module.sentry_sdk, "capture_exception", lambda e: None
    )
    reporter, attempts = await _run_failing(retry=False)
    assert attempts.count == 1
    assert len(reporter.alerts) == 1


async def test_a_driver_error_reaches_the_alert_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alert chat is one more place PostgreSQL's DETAIL must not reach."""
    monkeypatch.setattr(
        middlewares_module.sentry_sdk, "capture_exception", lambda e: None
    )
    reporter, _ = await _run_failing(
        retry=False,
        error=DBAPIError("UPDATE", {}, Exception("DETAIL: Key (id)=(4242)")),
    )
    [alert] = reporter.alerts
    assert isinstance(alert.error, ScrubbedError)
    assert "4242" not in str(alert.error)


async def test_a_task_cancelled_by_a_stopping_worker_does_not_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every deploy in the middle of a long task would page the developers."""
    monkeypatch.setattr(
        middlewares_module.sentry_sdk, "capture_exception", lambda e: None
    )
    reporter, attempts = await _run_failing(retry=False, error=asyncio.CancelledError())
    assert attempts.count == 1
    assert reporter.alerts == []


async def test_a_worker_without_a_reporter_only_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot's broker never runs tasks and has no reporter in its state."""
    monkeypatch.setattr(
        middlewares_module.sentry_sdk, "capture_exception", lambda e: None
    )
    broker = InMemoryBroker(await_inplace=True)
    broker.add_middlewares(TaskFailureAlertMiddleware())
    attempts = Attempts()
    broker.register_task(_failing_task(attempts), task_name="test.fail")
    await broker.startup()
    try:
        with captured_logs("src.core.tasks.middlewares") as records:
            await broker.find_task("test.fail").kiq()
    finally:
        await broker.shutdown()
    assert any("no failure reporter" in record.getMessage() for record in records)
