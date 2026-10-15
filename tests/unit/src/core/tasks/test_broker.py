from taskiq import TaskiqMiddleware
from taskiq.middlewares import SmartRetryMiddleware
from taskiq_redis import RedisStreamBroker

from src.core.redis.core import REDIS_SOCKET_TIMEOUT_SECONDS
from src.core.tasks.broker import (
    RETRY_DELAY_SECONDS,
    STREAM_IDLE_TIMEOUT_SECONDS,
    STREAM_MAXLEN,
    RetryScheduleSource,
    create_stream_broker,
)
from src.core.tasks.middlewares import SentryMiddleware

DSN = "redis://localhost:6379/1"


class First(TaskiqMiddleware):
    pass


def test_the_stream_broker_is_capped_and_retries_through_the_scheduler() -> None:
    first = First()
    broker = create_stream_broker(DSN, first)
    assert isinstance(broker, RedisStreamBroker)
    assert broker.maxlen == STREAM_MAXLEN
    assert broker.idle_timeout == STREAM_IDLE_TIMEOUT_SECONDS * 1000
    head, retry, sentry = broker.middlewares
    assert head is first
    assert isinstance(retry, SmartRetryMiddleware)
    assert retry.default_delay == RETRY_DELAY_SECONDS
    assert isinstance(retry.schedule_source, RetryScheduleSource)
    assert isinstance(sentry, SentryMiddleware)


def test_a_send_to_a_blackholed_redis_times_out_in_seconds() -> None:
    """The bot sends inside an update's commit; without socket timeouts a
    dead Redis would hold it for the kernel's TCP timeout. They stay above
    the worker's XREADGROUP block, or every idle read would time out."""
    broker = create_stream_broker(DSN)
    options = broker.connection_pool.connection_kwargs
    assert options["socket_timeout"] == REDIS_SOCKET_TIMEOUT_SECONDS
    assert options["socket_connect_timeout"] == REDIS_SOCKET_TIMEOUT_SECONDS
    assert broker.block / 1000 < REDIS_SOCKET_TIMEOUT_SECONDS
