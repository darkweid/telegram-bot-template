"""Every background task of the bot and how the broker is assembled.

A task missing from TASKS is invisible to the worker, and the outbox would
publish a name nobody runs.
"""

from taskiq import AsyncBroker, InMemoryBroker

from src.core.tasks.broker import create_stream_broker
from src.core.tasks.registry import TaskSpec, register_tasks
from src.main.config import Config

TASKS: tuple[TaskSpec, ...] = ()


def build_broker(config: Config) -> AsyncBroker:
    """The stream broker; under TESTING an in-memory one that runs a sent task
    inline, with no Redis involved."""
    broker: AsyncBroker = (
        InMemoryBroker(await_inplace=True)
        if config.app.TESTING
        else create_stream_broker(config.redis.tasks_dsn)
    )
    register_tasks(broker, TASKS)
    return broker
