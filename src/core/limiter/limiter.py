from collections.abc import Callable
from dataclasses import dataclass
import time

from redis.asyncio import Redis
import redis.exceptions as redis_exc

from loggers import get_logger
from src.core.limiter.script import RATE_LIMIT_SCRIPT
from src.core.redis.degradation import RedisDegradationReporter
from src.core.redis.errors import FAIL_OPEN_ERRORS

logger = get_logger(__name__)

# Around 20-25 MB of fallback state at most.
DEFAULT_FALLBACK_MAX_ENTRIES = 100_000


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass(slots=True)
class _Window:
    count: int
    expires_at_ms: int


class RateLimiter:
    """
    Fixed-window rate limiter backed by a Redis Lua script.

    When Redis is unreachable it counts in process memory instead, so an
    outage turns the limit per-instance rather than removing it, and reports
    the outage once per incident rather than per call.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str,
        reporter: RedisDegradationReporter,
        fallback_max_entries: int = DEFAULT_FALLBACK_MAX_ENTRIES,
        clock: Callable[[], int] = _monotonic_ms,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._reporter = reporter
        self._fallback_max_entries = fallback_max_entries
        self._clock = clock
        self._sha: str | None = None
        self._fallback_windows: dict[str, _Window] = {}

    async def hit(self, key: str, *, limit: int, window_ms: int) -> int:
        """Count one call against `key`. Returns 0 when the call is admitted,
        otherwise the milliseconds left until the window resets."""
        if limit < 1 or window_ms < 1:
            raise ValueError("limit and window_ms must be positive")
        try:
            result = await self._eval(f"{self._prefix}:{key}", limit, window_ms)
        except FAIL_OPEN_ERRORS as error:
            logger.warning(
                "[RateLimiter] Redis unavailable, counting in memory: %s", error
            )
            self._reporter.report_degraded(error)
            return self._hit_in_memory(key, limit, window_ms)
        # Only an admitted call writes (INCR/SET); a refused one reads with GET
        # and PTTL, which an out-of-memory Redis still answers. Recovering on a
        # refusal would close the incident on every flood.
        if result == 0:
            self._reporter.report_recovered()
        return result

    async def _eval(self, redis_key: str, limit: int, window_ms: int) -> int:
        if self._sha is None:
            self._sha = await self._redis.script_load(RATE_LIMIT_SCRIPT)
        try:
            result = await self._redis.evalsha(
                self._sha, 1, redis_key, str(limit), str(window_ms)
            )
        except redis_exc.NoScriptError:
            # SCRIPT FLUSH or a restarted Redis forgot the script.
            self._sha = await self._redis.script_load(RATE_LIMIT_SCRIPT)
            result = await self._redis.evalsha(
                self._sha, 1, redis_key, str(limit), str(window_ms)
            )
        return int(result)

    def _hit_in_memory(self, key: str, limit: int, window_ms: int) -> int:
        now_ms = self._clock()
        window = self._fallback_windows.get(key)
        if window is not None and window.expires_at_ms <= now_ms:
            del self._fallback_windows[key]
            window = None
        if window is None:
            self._make_room(now_ms)
            self._fallback_windows[key] = _Window(1, now_ms + window_ms)
            return 0
        if window.count + 1 > limit:
            return max(window.expires_at_ms - now_ms, 1)
        window.count += 1
        return 0

    def _make_room(self, now_ms: int) -> None:
        if len(self._fallback_windows) < self._fallback_max_entries:
            return
        expired = [
            k for k, w in self._fallback_windows.items() if w.expires_at_ms <= now_ms
        ]
        for expired_key in expired:
            del self._fallback_windows[expired_key]
        if len(self._fallback_windows) >= self._fallback_max_entries:
            oldest = min(
                self._fallback_windows,
                key=lambda k: self._fallback_windows[k].expires_at_ms,
            )
            del self._fallback_windows[oldest]
            logger.warning(
                "[RateLimiter] In-memory fallback full (%s keys), evicted the oldest",
                self._fallback_max_entries,
            )
