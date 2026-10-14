import asyncio
from collections.abc import Awaitable

from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from loggers import get_logger
from src.core.tasks.broker import TasksRedis
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


class HealthService:
    """Backs /ready and /health.

    Never reports to Sentry: the probes run on a timer, so one outage would
    file an event per poll, and the outage is what infrastructure alerting
    watches anyway.
    """

    def __init__(
        self,
        repository: SystemRepository,
        session: AsyncSession,
        redis: Redis,
        storage: BaseStorage,
        tasks_redis: TasksRedis,
        app_config: AppConfig,
        bot_config: BotConfig,
    ) -> None:
        self.repository = repository
        self.session = session
        self.redis = redis
        self.storage = storage
        self.tasks_redis = tasks_redis
        self.app_config = app_config
        self.bot_config = bot_config

    async def check_readiness(self) -> Readiness:
        # Concurrently, so the whole check stays within one probe budget.
        postgres, redis, fsm, tasks = await asyncio.gather(
            self._probe("Postgres", self.repository.ping(self.session)),
            self._probe("Redis", self.redis.ping()),
            self._probe("Redis FSM", self._ping_fsm_storage()),
            self._probe("Redis tasks", self.tasks_redis.ping()),
        )
        return Readiness(postgres=postgres, redis=redis and fsm and tasks)

    async def get_report(self) -> HealthReport:
        """Never raises: monitoring reads this to find out *which* dependency
        is down, so it must keep answering exactly when something is broken."""
        readiness = await self.check_readiness()
        ratio = await self._redis_memory_used_ratio() if readiness.redis else None
        healthy = readiness.ok and (
            ratio is None or ratio < REDIS_MEMORY_DEGRADED_RATIO
        )
        return HealthReport(
            status="ok" if healthy else "degraded",
            version=self.app_config.VERSION,
            mode=self.bot_config.BOT_MODE,
            postgres=readiness.postgres,
            redis=readiness.redis,
            redis_memory_used_ratio=ratio,
        )

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
        """None without a maxmemory cap, and when INFO cannot be read."""
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                memory = await self.redis.info("memory")
            used = int(memory["used_memory"])
            limit = int(memory["maxmemory"])
        except Exception as exc:
            logger.warning("Redis memory check failed: %s", type(exc).__name__)
            return None
        if limit <= 0:
            return None
        return round(used / limit, 4)
