from typing import Any

import sentry_sdk
from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

from src.core.errors.scrubbed import reportable


class SentryMiddleware(TaskiqMiddleware):
    """Report task failures to Sentry.

    There is no official taskiq integration. Worker errors have no ASGI
    middleware to catch them, so this is the single reporting point for
    exceptions escaping a task. Fires once per failed attempt, so a task
    retried by SmartRetryMiddleware reports each attempt.

    Exceptions reach Sentry through `reportable`: a driver error must never
    carry its DETAIL there.
    """

    def on_error(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
        exception: BaseException,
    ) -> None:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("taskiq.task_name", message.task_name)
            scope.set_context("taskiq", {"task_id": message.task_id})
            sentry_sdk.capture_exception(reportable(exception))
