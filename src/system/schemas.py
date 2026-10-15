from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Readiness:
    postgres: bool
    # Every Redis client answers: the cache's database, the FSM's and the task
    # queue's.
    redis: bool

    @property
    def ok(self) -> bool:
        # The bot needs both: PostgreSQL for users, Redis for FSM state.
        return self.postgres and self.redis


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The detailed report served at /health for monitoring.

    Always served with 200, including while a dependency is down - that is the
    one moment the per-dependency detail matters. Orchestrators read /live and
    /ready instead, which turn an outage into a status code.
    """

    status: Literal["ok", "degraded"]
    version: str
    mode: str
    postgres: bool
    redis: bool
    # used_memory / maxmemory; None when Redis is down or has no memory cap.
    # Under `noeviction` a full Redis refuses writes, so FSM state stops saving
    # before anything is down.
    redis_memory_used_ratio: float | None
    commit: str
    # PENDING outbox rows and the age of the oldest; None when PostgreSQL is
    # down or the count failed.
    outbox_pending: int | None
    outbox_oldest_pending_seconds: float | None
    # Tasks waiting in the workers' consumer group: not yet delivered plus
    # delivered and not acked. None when any Redis client is down (readiness),
    # when the read fails or times out, or when Redis cannot count the lag.
    task_queue_length: int | None
    # Since the scheduler's last heartbeat; None when none was ever written,
    # when any Redis client is down (readiness), or when the read fails or
    # times out.
    scheduler_heartbeat_age_seconds: float | None
