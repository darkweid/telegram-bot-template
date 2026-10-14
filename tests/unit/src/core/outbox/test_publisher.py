from typing import Any
from uuid import uuid4

import pytest
from taskiq import Context, InMemoryBroker, TaskiqDepends

from src.core.outbox.publisher import UnregisteredTaskError, kick_stored

# A shared marker, not a call in the default: taskiq only reads it.
TASK_CONTEXT = TaskiqDepends()
RECEIVED: list[tuple[str, dict[str, Any], tuple[Any, ...], dict[str, Any]]] = []


async def received_task(
    *args: Any, context: Context = TASK_CONTEXT, **kwargs: Any
) -> None:
    RECEIVED.append((context.message.task_id, kwargs, args, context.message.labels))


async def test_a_stored_task_is_sent_under_its_row_id_with_its_labels() -> None:
    """The worker deduplicates on the task id, and a retried task needs
    retry_on_error to travel with it."""
    RECEIVED.clear()
    broker = InMemoryBroker(await_inplace=True)
    broker.register_task(received_task, task_name="test.task", retry_on_error=True)
    message_id = uuid4()
    await broker.startup()
    try:
        await kick_stored(broker, message_id, "test.task", [1], {"user_id": 2})
    finally:
        await broker.shutdown()
    [(task_id, kwargs, args, labels)] = RECEIVED
    assert task_id == str(message_id)
    assert kwargs == {"user_id": 2}
    assert args == (1,)
    assert labels["retry_on_error"] is True


async def test_an_unregistered_task_is_refused() -> None:
    """A name no worker runs would sit in the stream forever."""
    with pytest.raises(UnregisteredTaskError, match="test.missing"):
        await kick_stored(InMemoryBroker(), uuid4(), "test.missing", [], {})
