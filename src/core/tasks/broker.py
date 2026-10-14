from typing import NewType

from redis.asyncio import Redis
from taskiq import TaskiqEvents, TaskiqMiddleware, TaskiqState
from taskiq.middlewares import SmartRetryMiddleware
from taskiq_redis import ListRedisScheduleSource, RedisStreamBroker

from src.core.redis.core import REDIS_SOCKET_TIMEOUT_SECONDS
from src.core.tasks.middlewares import SentryMiddleware

# The client of REDIS_TASKS_DATABASE: the broker's streams, retry schedules and
# the scheduler heartbeat live there, apart from the cache and FSM state.
TasksRedis = NewType("TasksRedis", Redis)

STREAM_QUEUE_NAME = "taskiq"

# taskiq_redis never removes acked entries (XACK only clears the consumer
# group's pending-entries list), so an uncapped stream grows forever. XADD
# MAXLEN is the only bound, but it trims by ID order alone and does not check
# ack state, so it can discard unacknowledged work once the backlog reaches
# this size. Task payloads here are small, so this is set high enough that no
# realistic worker outage or traffic burst should approach it - it guards
# against runaway growth, not against a live backlog.
STREAM_MAXLEN = 100_000

RETRY_DELAY_SECONDS = 60

# How long a delivered, unacked message waits before another worker reclaims it
# (XAUTOCLAIM). taskiq_redis defaults to the same ten minutes; it is spelled out
# because the receiver's running claim is sized against it.
STREAM_IDLE_TIMEOUT_SECONDS = 600


class RetryScheduleSource(ListRedisScheduleSource):
    # taskiq_redis opens a connection pool per source and never disconnects
    # it: ListRedisScheduleSource has no shutdown of its own.
    async def shutdown(self) -> None:
        await super().shutdown()
        await self._connection_pool.disconnect()

    async def on_worker_shutdown(self, _state: TaskiqState) -> None:
        """SmartRetryMiddleware writes retries through this source's own pool
        in the worker; the scheduler closes it through its sources instead."""
        await self.shutdown()


def create_stream_broker(dsn: str, *first: TaskiqMiddleware) -> RedisStreamBroker:
    """The Redis Streams broker of the bot, the worker and the scheduler.

    `first` go before the retry middleware: taskiq runs `on_error` in reverse,
    so theirs runs after SmartRetryMiddleware has decided whether this attempt
    was the last one.

    No result backend on purpose: nothing reads task results, and the default
    dummy backend keeps no pickled payloads in Redis.
    """
    broker = RedisStreamBroker(
        url=dsn,
        queue_name=STREAM_QUEUE_NAME,
        maxlen=STREAM_MAXLEN,
        approximate=True,
        idle_timeout=STREAM_IDLE_TIMEOUT_SECONDS * 1000,
        # The project clients' timeouts: without them a blackholed Redis holds
        # a send for the kernel's TCP timeout. Above the 2 s XREADGROUP block
        # of the worker's listen loop, which would otherwise time out idle.
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    )
    # Retries are scheduled onto the source at now + delay; without a schedule
    # source RedisStreamBroker ignores the `delay` label and every retry fires
    # back to back. Delayed retries therefore need the scheduler running.
    retry_source = RetryScheduleSource(dsn)
    broker.add_middlewares(
        *first,
        SmartRetryMiddleware(
            default_retry_count=3,
            default_delay=RETRY_DELAY_SECONDS,
            use_jitter=True,
            schedule_source=retry_source,
        ),
        SentryMiddleware(),
    )
    broker.add_event_handler(
        TaskiqEvents.WORKER_SHUTDOWN, retry_source.on_worker_shutdown
    )
    return broker
