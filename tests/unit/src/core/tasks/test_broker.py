from taskiq import TaskiqMiddleware
from taskiq.middlewares import SmartRetryMiddleware
from taskiq_redis import RedisStreamBroker

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
