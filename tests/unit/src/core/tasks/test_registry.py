from sqlalchemy.exc import DBAPIError
from taskiq import InMemoryBroker

from src.core.errors.scrubbed import ScrubbedError
from src.core.tasks.registry import TaskSpec, register_tasks


async def ping(value: int) -> int:
    return value + 1


async def broken() -> None:
    raise DBAPIError("UPDATE", {}, Exception("DETAIL: Key (id)=(4242)"))


async def test_a_registered_task_carries_its_name_schedule_and_retry_label() -> None:
    broker = InMemoryBroker(await_inplace=True)
    register_tasks(
        broker,
        [
            TaskSpec(
                "test.ping",
                ping,
                schedule=({"cron": "* * * * *"},),
                retry_on_error=True,
            ),
            TaskSpec("test.plain", ping),
        ],
    )
    task = broker.find_task("test.ping")
    assert task is not None
    assert task.labels == {"schedule": [{"cron": "* * * * *"}], "retry_on_error": True}
    plain = broker.find_task("test.plain")
    assert plain is not None and plain.labels == {}


async def test_a_task_error_leaves_the_task_already_scrubbed() -> None:
    """taskiq's receiver logs a failed task before any middleware sees it."""
    broker = InMemoryBroker(await_inplace=True)
    register_tasks(broker, [TaskSpec("test.broken", broken)])
    await broker.startup()
    try:
        task = await broker.find_task("test.broken").kiq()
        result = await task.wait_result()
    finally:
        await broker.shutdown()
    assert isinstance(result.error, ScrubbedError)
    assert "4242" not in str(result.error)


async def refused() -> None:
    raise PermissionError("not allowed")


async def test_an_error_without_user_data_leaves_the_task_unchanged() -> None:
    """Retry rules and alerts match on the task's own exception type."""
    broker = InMemoryBroker(await_inplace=True)
    register_tasks(broker, [TaskSpec("test.refused", refused)])
    await broker.startup()
    try:
        task = await broker.find_task("test.refused").kiq()
        result = await task.wait_result()
    finally:
        await broker.shutdown()
    assert isinstance(result.error, PermissionError)
