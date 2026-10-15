from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import wraps
from typing import Any

from dishka.integrations.taskiq import inject
from taskiq import AsyncBroker

from src.core.errors.scrubbed import reportable

TaskFunction = Callable[..., Awaitable[Any]]

AT_MOST_ONCE_LABEL = "at_most_once"


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """A background task: the name the outbox stores, the function, and how
    taskiq runs it. `schedule` is taskiq's label (`[{"cron": "* * * * *"}]`);
    `retry_on_error` opts into SmartRetryMiddleware; `at_most_once` makes the
    worker record the task as done before it runs, so a delivery reclaimed from
    a worker that stopped midway is dropped instead of run again (for a message
    a person reads, where a lost one beats a repeated one)."""

    name: str
    func: TaskFunction
    schedule: tuple[dict[str, str], ...] = ()
    retry_on_error: bool = False
    at_most_once: bool = False

    def __post_init__(self) -> None:
        # A retry is sent under the same task id, so the marker written before
        # the first run would drop every retry.
        if self.at_most_once and self.retry_on_error:
            raise ValueError(f"Task {self.name}: at_most_once cannot be retried")


def register_tasks(broker: AsyncBroker, tasks: Iterable[TaskSpec]) -> None:
    """Register every task on `broker`, with dishka injection and scrubbed errors.

    Each process registers the same tasks: the worker runs them, the scheduler
    reads their schedules, the bot finds their labels when it sends one.
    """
    for spec in tasks:
        labels: dict[str, Any] = {}
        if spec.schedule:
            labels["schedule"] = list(spec.schedule)
        if spec.retry_on_error:
            labels["retry_on_error"] = True
        if spec.at_most_once:
            labels[AT_MOST_ONCE_LABEL] = True
        broker.register_task(
            inject(_scrub_errors(spec.func), patch_module=True),
            task_name=spec.name,
            **labels,
        )


def _scrub_errors(func: TaskFunction) -> TaskFunction:
    """taskiq's receiver logs a failed task, chain and all, before any
    middleware runs; the error must already be safe when it leaves the task."""

    @wraps(func)
    async def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            safe = reportable(exc)
            if safe is exc:
                raise
        # Raised outside the handler: inside it, `exc` with its DETAIL would
        # become `safe.__context__`, and `from None` would drop the scrubbed
        # cause `reportable` kept.
        raise safe

    return run
