from collections.abc import Sequence
from typing import Any, cast

from redis.asyncio import Redis
import redis.exceptions as redis_exc

from loggers import get_logger
from src.core.cache.base import BaseCache
from src.core.cache.interface import CacheKey, Serializer
from src.core.cache.redis_scripts import (
    CACHE_DELETE_SCRIPT,
    CACHE_GET_SCRIPT,
    CACHE_INVALIDATE_SCRIPT,
    CACHE_SET_SCRIPT,
)
from src.core.redis.degradation import RedisDegradationReporter
from src.core.redis.errors import FAIL_OPEN_ERRORS

logger = get_logger(__name__)


class RedisCache(BaseCache):
    """
    Redis-backed cache with namespace versioning.

    Reads and writes go through Lua so that resolving every version counter the
    key composes from - the namespace and each of its tags - and touching the
    value happen in a single round trip. Transport failures and OOM refusals are
    swallowed (a cache outage must not fail an update), except by
    `delete_or_raise`, whose caller asked to know; programmer errors -
    unserializable values, a ttl outside (0, version_ttl], a rejected command -
    are raised. Only a successful write reports recovery.

    A delete that failed open leaves its key marked in this process: once Redis
    is back the old value is still there, and serving it would undo the change
    the delete was for (a ban let through by a stale snapshot). A read of a
    marked key retries the delete and reports a miss whatever the outcome; a
    delete that lands or a successful write (a value read from the database
    after the change) clears the mark. The set holds only keys whose delete
    failed, so it stays as small as the outage. It lives in memory because the
    bot is one process; a restart during the outage loses it, and the caller's
    outbox task (`delete_or_raise`) covers that.
    """

    def __init__(
        self,
        redis_client: Redis,
        serializer: Serializer,
        *,
        prefix: str,
        default_ttl: int,
        version_ttl: int,
        enabled: bool = True,
        reporter: RedisDegradationReporter | None = None,
    ) -> None:
        super().__init__(
            serializer,
            prefix=prefix,
            default_ttl=default_ttl,
            version_ttl=version_ttl,
            enabled=enabled,
        )
        self._redis = redis_client
        self._reporter = reporter or RedisDegradationReporter("Cache")
        self._failed_deletes: set[CacheKey] = set()

    def _namespace_prefix(self, namespace: str) -> str:
        return f"{self._prefix}:{namespace}"

    async def _eval(self, script: str, counters: Sequence[str], *args: Any) -> Any:
        return await self._redis.eval(script, len(counters), *counters, *args)

    async def _read_raw(self, key: CacheKey) -> str | None:
        if key in self._failed_deletes:
            # The stored value predates a change whose delete was lost: it is
            # never served, only dropped once Redis takes the delete.
            await self._drop(key)
            return None
        try:
            raw = await self._eval(
                CACHE_GET_SCRIPT,
                self._counter_keys(key),
                self._namespace_prefix(key.namespace),
                key.suffix,
            )
        except FAIL_OPEN_ERRORS as error:
            self._on_redis_outage("read", key, error)
            return None
        if raw is None:
            logger.debug("[Cache] miss %s", key)
            return None
        logger.debug("[Cache] hit %s", key)
        return cast(str, raw)

    async def _write_raw(self, key: CacheKey, raw: str, ttl: int) -> None:
        try:
            await self._eval(
                CACHE_SET_SCRIPT,
                self._counter_keys(key),
                self._namespace_prefix(key.namespace),
                key.suffix,
                raw,
                str(ttl),
                str(self._version_ttl),
            )
        except FAIL_OPEN_ERRORS as error:
            self._on_redis_outage("write", key, error)
            return
        self._failed_deletes.discard(key)
        # Only a write proves recovery: under OOM a read or a DEL still succeeds,
        # so recovering on one would restart the cooldown on every miss.
        self._reporter.report_recovered()

    async def _drop(self, key: CacheKey) -> None:
        try:
            await self._drop_or_raise(key)
        except FAIL_OPEN_ERRORS as error:
            self._failed_deletes.add(key)
            self._on_redis_outage("delete", key, error)

    async def _drop_or_raise(self, key: CacheKey) -> None:
        await self._eval(
            CACHE_DELETE_SCRIPT,
            self._counter_keys(key),
            self._namespace_prefix(key.namespace),
            key.suffix,
        )
        self._failed_deletes.discard(key)

    async def _retire_counters(self, counters: Sequence[str]) -> None:
        try:
            await self._eval(CACHE_INVALIDATE_SCRIPT, counters)
            logger.debug("[Cache] counters %s retired", list(counters))
        except FAIL_OPEN_ERRORS as error:
            logger.warning(
                "[Cache] invalidate failed for counters %s: %s", list(counters), error
            )
            self._reporter.report_degraded(error)

    def _on_redis_outage(
        self, operation: str, key: CacheKey, error: redis_exc.RedisError
    ) -> None:
        logger.warning("[Cache] %s failed for %s: %s", operation, key, error)
        self._reporter.report_degraded(error)
