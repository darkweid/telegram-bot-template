import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from redis import exceptions as redis_exceptions
from taskiq import Context, InMemoryBroker, TaskiqDepends
from taskiq.exceptions import SendTaskError
from taskiq.message import BrokerMessage

from src.core.outbox import publisher as publisher_module
from src.core.outbox.publisher import (
    OutboxPublisher,
    UnregisteredTaskError,
    is_broker_unreachable,
    kick_stored,
)
from tests.helpers.logs import captured_logs

# A shared marker, not a call in the default: taskiq only reads it.
TASK_CONTEXT = TaskiqDepends()
RECEIVED: list[tuple[str, dict[str, Any], tuple[Any, ...], dict[str, Any]]] = []


def _sent(cause: Exception) -> SendTaskError:
    """The error as taskiq's kicker raises it."""
    error = SendTaskError()
    error.__cause__ = cause
    return error


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


class HangingBroker(InMemoryBroker):
    """Accepts the connection and never answers, like a blackholed Redis."""

    async def kick(self, message: BrokerMessage) -> None:
        await asyncio.Event().wait()


class DownBroker(InMemoryBroker):
    """Cannot even start: the tasks Redis is down."""

    def __init__(self) -> None:
        super().__init__(await_inplace=True)
        self.starts = 0

    async def startup(self) -> None:
        self.starts += 1
        # Yields first, as a refused connection does: concurrent sends queue.
        await asyncio.sleep(0)
        raise redis_exceptions.ConnectionError("tasks Redis down")


class HangingStartBroker(InMemoryBroker):
    """Its start never answers, like a blackholed tasks Redis."""

    def __init__(self) -> None:
        super().__init__(await_inplace=True)
        self.starts = 0

    async def startup(self) -> None:
        self.starts += 1
        await asyncio.Event().wait()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ConnectionError(),
        redis_exceptions.ConnectionError(),
        redis_exceptions.TimeoutError(),
        redis_exceptions.OutOfMemoryError(),
        _sent(redis_exceptions.ConnectionError()),
        _sent(redis_exceptions.OutOfMemoryError()),
    ],
)
def test_an_outage_is_the_brokers_fault(error: BaseException) -> None:
    """Counted against the message, an outage would fail every pending row;
    a raw error (our own send timeout) is classified like a wrapped one."""
    assert is_broker_unreachable(error)


@pytest.mark.parametrize(
    "error", [ValueError(), _sent(ValueError()), redis_exceptions.ResponseError()]
)
def test_a_refusal_is_the_messages_fault(error: BaseException) -> None:
    assert not is_broker_unreachable(error)


async def test_a_hanging_send_gives_up_within_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The send runs inside an update's commit: a blackholed Redis must not
    hold the update for the kernel's TCP timeout."""
    monkeypatch.setattr(publisher_module, "SEND_TIMEOUT_SECONDS", 0.05)
    broker = HangingBroker(await_inplace=True)
    broker.register_task(received_task, task_name="test.task")
    sessionmaker = MagicMock()
    publisher = OutboxPublisher(broker, sessionmaker)
    with captured_logs("src.core.outbox.publisher") as records:
        async with asyncio.timeout(5):
            await publisher.publish(uuid4(), "test.task", [], {})
    # Not marked published: the row stays pending for the sweeper.
    sessionmaker.assert_not_called()
    [record] = records
    assert record.levelno == logging.WARNING
    assert "TimeoutError" in record.getMessage()


async def test_a_broker_that_cannot_start_leaves_the_row_and_retries_the_start() -> (
    None
):
    """The bot starts its broker on the first send; an outage then must cost
    the send, not the update, and the next send tries again."""
    broker = DownBroker()
    sessionmaker = MagicMock()
    publisher = OutboxPublisher(broker, sessionmaker, start_broker=True)
    await publisher.publish(uuid4(), "test.task", [], {})
    await publisher.publish(uuid4(), "test.task", [], {})
    assert broker.starts == 2
    sessionmaker.assert_not_called()


async def test_a_started_broker_is_not_started_again() -> None:
    """The worker's broker belongs to the taskiq CLI: a second startup would
    rerun the worker's startup hooks."""
    broker = DownBroker()
    publisher = OutboxPublisher(broker, MagicMock())
    with pytest.raises(UnregisteredTaskError):
        await publisher.publish(uuid4(), "test.missing", [], {})
    assert broker.starts == 0


async def test_concurrent_first_sends_share_one_bounded_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each send waits in an update's after-commit hook: queued behind one
    another, N sends would hold the Nth update N bounds, past its lock."""
    monkeypatch.setattr(publisher_module, "SEND_TIMEOUT_SECONDS", 0.2)
    broker = HangingStartBroker()
    sessionmaker = MagicMock()
    publisher = OutboxPublisher(broker, sessionmaker, start_broker=True)
    loop = asyncio.get_running_loop()
    began = loop.time()
    async with asyncio.timeout(5):
        await asyncio.gather(
            *(publisher.publish(uuid4(), "test.task", [], {}) for _ in range(5))
        )
    assert loop.time() - began < 0.6
    assert broker.starts == 1
    sessionmaker.assert_not_called()


async def test_sends_queued_behind_a_failed_start_leave_their_rows() -> None:
    """A start that just failed says the broker is down: the sends that
    waited for it go to the sweeper instead of starting it again each."""
    broker = DownBroker()
    sessionmaker = MagicMock()
    publisher = OutboxPublisher(broker, sessionmaker, start_broker=True)
    with captured_logs("src.core.outbox.publisher") as records:
        await asyncio.gather(
            *(publisher.publish(uuid4(), "test.task", [], {}) for _ in range(3))
        )
    assert broker.starts == 1
    assert [r.levelno for r in records] == [logging.WARNING] * 3
    sessionmaker.assert_not_called()
    # Later sends try again.
    await publisher.publish(uuid4(), "test.task", [], {})
    assert broker.starts == 2
