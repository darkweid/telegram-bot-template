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
