"""Every background task of the bot and how the broker is assembled.

A task missing from TASKS is invisible to the worker, and the outbox would
publish a name nobody runs.
"""

from taskiq import AsyncBroker, InMemoryBroker

from src.core.outbox.sweeper import purge_outbox, sweep_outbox
from src.core.tasks.broker import create_stream_broker
from src.core.tasks.registry import TaskSpec, register_tasks
from src.main.config import Config

TASKS: tuple[TaskSpec, ...] = (
    # Every minute: rows whose after-commit publish failed reach the worker
    # within a couple of minutes.
    TaskSpec("outbox.sweep", sweep_outbox, schedule=({"cron": "* * * * *"},)),
    TaskSpec("outbox.purge", purge_outbox, schedule=({"cron": "0 3 * * *"},)),
)


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
