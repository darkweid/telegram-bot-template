from typing import Any

import pytest
import sentry_sdk
from sqlalchemy.exc import DBAPIError
from taskiq import TaskiqMessage, TaskiqResult

from src.core.errors.scrubbed import ScrubbedError
from src.core.tasks.middlewares import SentryMiddleware


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
