import asyncio
from typing import Any

import sentry_sdk
from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult
from taskiq.exceptions import NoResultError

from loggers import get_logger
from src.core.errors.scrubbed import reportable
from src.core.telegram.alerts import Alert, TaskFailureReporter

logger = get_logger(__name__)

# Where SentryMiddleware leaves the event id for the alert of the same attempt.
SENTRY_EVENT_ID_LABEL = "sentry_event_id"
# The worker puts its DeveloperAlerter into broker.state under this key on startup.
FAILURE_REPORTER_STATE = "failure_reporter"


class SentryMiddleware(TaskiqMiddleware):
    """Report task failures to Sentry.

    There is no official taskiq integration. Worker errors have no ASGI
    middleware to catch them, so this is the single reporting point for
    exceptions escaping a task. Fires once per failed attempt, so a task
    retried by SmartRetryMiddleware reports each attempt. A task cancelled
    by a stopping worker is not a failure and is not reported.

    Exceptions reach Sentry through `reportable`: a driver error must never
    carry its DETAIL there. The event id is left in the attempt's result
    labels for TaskFailureAlertMiddleware, which runs after this one.
    """

    def on_error(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
        exception: BaseException,
    ) -> None:
        if isinstance(exception, asyncio.CancelledError):
            return
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("taskiq.task_name", message.task_name)
            scope.set_context("taskiq", {"task_id": message.task_id})
            event_id = sentry_sdk.capture_exception(reportable(exception))
        if event_id is not None:
            result.labels[SENTRY_EVENT_ID_LABEL] = event_id


class TaskFailureAlertMiddleware(TaskiqMiddleware):
    """Alert developers when a task has failed for good.

    Goes first in the broker's middleware list: taskiq runs `on_error` in
    reverse, so by the time it runs SmartRetryMiddleware has decided. A
    scheduled retry replaces the result's error with NoResultError; anything
    else means this attempt was the last. A task cancelled by a stopping
    worker has not failed.
    """

    async def on_error(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
        exception: BaseException,
    ) -> None:
        if isinstance(result.error, NoResultError) or isinstance(
            exception, asyncio.CancelledError
        ):
            return
        reporter: TaskFailureReporter | None = self.broker.state.get(
            FAILURE_REPORTER_STATE
        )
        if reporter is None:
            logger.warning("Task %s failed; no failure reporter", message.task_name)
            return
        await reporter.alert_error(
            Alert(
                reportable(exception),
                origin=f"task {message.task_name}",
                sentry_event_id=result.labels.get(SENTRY_EVENT_ID_LABEL),
                task_id=message.task_id,
            )
        )
