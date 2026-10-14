import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import partial

from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from loggers import get_logger
from src.core.database.detached import detached_read
from src.core.outbox.repositories import OutboxRepository
from src.core.tasks.broker import STREAM_CONSUMER_GROUP, STREAM_QUEUE_NAME, TasksRedis
from src.core.tasks.heartbeat import HEARTBEAT_TTL_SECONDS, last_beat_age
from src.core.utils.datetime_utils import get_utc_now
from src.main.config import AppConfig, BotConfig
from src.system.repositories import SystemRepository
from src.system.schemas import HealthReport, Readiness

logger = get_logger(__name__)

# A probe must answer in bounded time: a dependency that hangs instead of
# refusing would otherwise keep the orchestrator waiting on /ready.
PROBE_TIMEOUT_SECONDS = 2.0

# Share of maxmemory past which /health reports "degraded". Redis runs with
# `noeviction` (infra/redis.conf), so at 100% every write fails; this leaves
# monitoring room to act first.
REDIS_MEMORY_DEGRADED_RATIO = 0.9

# Age of the oldest PENDING outbox row past which /health reports "degraded".
# The sweeper retries every minute after a 60-second grace; rows this old mean
# publishing keeps failing.
OUTBOX_STALE_SECONDS = 300


class StreamLagUnknownError(Exception):
    """Redis cannot count the group's undelivered entries (XINFO GROUPS lag is
    nil after some trims and deletions); the reading is skipped, not guessed."""


class HealthService:
    """Backs /ready and /health.

    Never reports to Sentry: the probes run on a timer, so one outage would
    file an event per poll, and the outage is what infrastructure alerting
    watches anyway.

    /health also reports the outbox backlog, the task queue length and the
    scheduler heartbeat; a silent scheduler or an outbox stuck past
    OUTBOX_STALE_SECONDS makes it "degraded".
    """

    def __init__(
        self,
        repository: SystemRepository,
        outbox: OutboxRepository,
        session: AsyncSession,
        redis: Redis,
        storage: BaseStorage,
        tasks_redis: TasksRedis,
        app_config: AppConfig,
        bot_config: BotConfig,
    ) -> None:
        self.repository = repository
        self.outbox = outbox
        self.session = session
        self.redis = redis
        self.storage = storage
        self.tasks_redis = tasks_redis
        self.app_config = app_config
        self.bot_config = bot_config

    async def check_readiness(self) -> Readiness:
        # Concurrently, so the whole check stays within one probe budget.
        postgres, redis, fsm, tasks = await asyncio.gather(
            self._probe("Postgres", self._ping_postgres()),
            self._probe("Redis", self.redis.ping()),
            self._probe("Redis FSM", self._ping_fsm_storage()),
            self._probe("Redis tasks", self.tasks_redis.ping()),
        )
        return Readiness(postgres=postgres, redis=redis and fsm and tasks)

    async def get_report(self) -> HealthReport:
        """Never raises: monitoring reads this to find out *which* dependency
        is down, so it must keep answering exactly when something is broken."""
        readiness = await self.check_readiness()
        now = get_utc_now()
        backlog, ratio, queue_length, beat_age = await asyncio.gather(
            self._measure("Outbox backlog", self._outbox_backlog, readiness.postgres),
            self._measure(
                "Redis memory", self._redis_memory_used_ratio, readiness.redis
            ),
            self._measure("Task queue", self._task_backlog, readiness.redis),
            self._measure(
                "Scheduler heartbeat",
                partial(last_beat_age, self.tasks_redis, now),
                readiness.redis,
            ),
        )
        pending, oldest = backlog or (None, None)
        healthy = (
            readiness.ok
            and (ratio is None or ratio < REDIS_MEMORY_DEGRADED_RATIO)
            and beat_age is not None
            and beat_age.total_seconds() <= HEARTBEAT_TTL_SECONDS
            and (oldest is None or oldest.total_seconds() <= OUTBOX_STALE_SECONDS)
        )
        return HealthReport(
            status="ok" if healthy else "degraded",
            version=self.app_config.VERSION,
            mode=self.bot_config.BOT_MODE,
            postgres=readiness.postgres,
            redis=readiness.redis,
            redis_memory_used_ratio=ratio,
            commit=self.app_config.COMMIT_SHA,
            outbox_pending=pending,
            outbox_oldest_pending_seconds=_seconds(oldest),
            task_queue_length=queue_length,
            scheduler_heartbeat_age_seconds=_seconds(beat_age),
        )

    async def _measure[T](
        self, name: str, read: Callable[[], Awaitable[T]], available: bool
    ) -> T | None:
        """One reading for the report: None when its dependency is down, when
        it fails, or when it takes longer than a probe may."""
        if not available:
            return None
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                return await read()
        except Exception as exc:
            logger.warning("%s check failed: %s", name, type(exc).__name__)
            return None

    async def _ping_postgres(self) -> None:
        async with detached_read(self.session):
            await self.repository.ping(self.session)

    async def _outbox_backlog(self) -> tuple[int, timedelta | None]:
        # One after the other: a session serves one statement at a time.
        async with detached_read(self.session):
            pending = await self.outbox.count_pending(self.session)
            oldest = await self.outbox.oldest_pending_age(self.session)
        return pending, oldest

    async def _ping_fsm_storage(self) -> None:
        """FSM state has a client and a database of its own (REDIS_FSM_DATABASE):
        an index the server refuses fails this client alone, on every update,
        while the cache client still answers."""
        # Any other storage keeps its state in the process: nothing to reach.
        if isinstance(self.storage, RedisStorage):
            await self.storage.redis.ping()

    async def _probe(self, name: str, check: Awaitable[object]) -> bool:
        """Catches everything on purpose: a host that does not resolve reaches
        the probe as a bare socket.gaierror, never wrapped by the driver, and a
        probe that raises on an unreachable dependency defeats its own point."""
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                await check
            return True
        except Exception as exc:
            logger.warning("%s health check failed: %s", name, type(exc).__name__)
            return False

    async def _redis_memory_used_ratio(self) -> float | None:
        """None without a maxmemory cap."""
        memory = await self.redis.info("memory")
        used = int(memory["used_memory"])
        limit = int(memory["maxmemory"])
        if limit <= 0:
            return None
        return round(used / limit, 4)

    async def _task_backlog(self) -> int:
        """Tasks not yet delivered to a worker plus those delivered and not
        acked. Not XLEN: acked entries stay in the stream until MAXLEN trims
        them, so the stream's length is history, not work waiting."""
        if not await self.tasks_redis.exists(STREAM_QUEUE_NAME):
            return 0
        for group in await self.tasks_redis.xinfo_groups(STREAM_QUEUE_NAME):
            if group["name"] != STREAM_CONSUMER_GROUP:
                continue
            if group["lag"] is None:
                raise StreamLagUnknownError
            return int(group["lag"]) + int(group["pending"])
        # No broker has started yet: the group is created at "$" on the first
        # startup, so nothing written before it is waiting for a worker.
        return 0


def _seconds(age: timedelta | None) -> float | None:
    return None if age is None else round(age.total_seconds(), 1)
