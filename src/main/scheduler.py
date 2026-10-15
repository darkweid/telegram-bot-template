"""Scheduler entrypoint: `taskiq scheduler src.main.scheduler:scheduler`.

Run exactly one: taskiq has no duplicate-fire protection, N schedulers fire
every cron N times. Delayed retries flow through this process too.
"""

from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource

from loggers import setup_logging
from src.core.tasks.broker import RetryScheduleSource
from src.core.tasks.heartbeat import HeartbeatScheduleSource
from src.main.config import Config, get_settings
from src.main.tasks import build_broker


def configure_logging(_state: TaskiqState) -> None:
    setup_logging()


def build_scheduler(config: Config) -> TaskiqScheduler:
    broker = build_broker(config)
    # The scheduler starts its broker as a client; that is the first moment
    # inside its loop.
    broker.add_event_handler(TaskiqEvents.CLIENT_STARTUP, configure_logging)
    return TaskiqScheduler(
        broker=broker,
        sources=[
            LabelScheduleSource(broker),
            RetryScheduleSource(config.redis.tasks_dsn),
            HeartbeatScheduleSource(config.redis.tasks_dsn),
        ],
    )


scheduler = build_scheduler(get_settings())
