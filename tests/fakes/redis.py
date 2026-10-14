from __future__ import annotations

import asyncio
from collections.abc import Callable
import fnmatch
import hashlib
import time
from typing import Any

import redis.exceptions as redis_exc

from src.core.limiter.script import RATE_LIMIT_SCRIPT


def _normalize_key(key: str | bytes) -> str:
    if isinstance(key, bytes):
        return key.decode("utf-8")
    return key


def _normalize_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _now() -> float:
    return time.monotonic()


async def _round_trip() -> None:
    """Yield the way a real command does, so concurrent callers interleave.

    Without this every method here runs start to finish before the event loop
    looks at anyone else, and a race test passes against an implementation that
    reads and writes in two round trips - which is the bug those tests exist to
    catch. Script bodies must not call this: Redis runs a script as one unit.
    """
    await asyncio.sleep(0)


class InMemoryRedis:
    """An in-process Redis for unit tests. Scripts are emulated one by one, each
    mirroring its Lua file; every emulation has an integration test running the
    real script against the same cases."""

    def __init__(self) -> None:
        # Wall clock behind TIME/time(): tests pin it to a fixed value. Key
        # expiry stays on time.monotonic() and is unaffected by reassigning this.
        self.wall_clock: Callable[[], float] = time.time
        self._store: dict[str, str] = {}
        self._expires: dict[str, float] = {}
        self._scripts: dict[str, str] = {}
        # Keys every evalsha ran against, in order. The rate limiter is the only
        # evalsha user, so this doubles as a record of which limiter windows a
        # request actually consumed.
        self.evalsha_keys: list[str] = []
        self.closed = False
        self._failures = 0
        self._failure_error: Exception = redis_exc.ConnectionError("fake redis down")
        # A full instance under noeviction. Real Redis refuses only commands that
        # may grow memory, so only a script's first write of a new key is
        # refused: reads, DEL and EXPIRE keep working, and a fake refusing them
        # too would hide code that treats a successful read as proof the
        # instance recovered.
        self.out_of_memory = False

    def fail_next_commands(
        self, count: int = 1, *, error: Exception | None = None
    ) -> None:
        self._failures = count
        self._failure_error = error or redis_exc.ConnectionError("fake redis down")

    async def _check_failure(self) -> None:
        await _round_trip()
        if self._failures > 0:
            self._failures -= 1
            raise self._failure_error

    def _purge_expired(self, key: str) -> None:
        expires_at = self._expires.get(key)
        if expires_at is None:
            return
        if _now() >= expires_at:
            self._store.pop(key, None)
            self._expires.pop(key, None)

    def _read(self, key: str) -> str | None:
        """The store as a script sees it: no yield, no round trip."""
        self._purge_expired(key)
        return self._store.get(key)

    def _write(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        self._store[key] = value
        if ttl_seconds is not None:
            self._expires[key] = _now() + int(ttl_seconds)

    def _drop(self, key: str) -> int:
        self._purge_expired(key)
        if key not in self._store:
            return 0
        self._store.pop(key, None)
        self._expires.pop(key, None)
        return 1

    def _expire(self, key: str, seconds: int) -> None:
        if key in self._store:
            self._expires[key] = _now() + int(seconds)

    def _expire_ms(self, key: str, milliseconds: int) -> None:
        if key in self._store:
            self._expires[key] = _now() + milliseconds / 1000

    def _pttl(self, key: str) -> int:
        self._purge_expired(key)
        if key not in self._store:
            return -2
        expires_at = self._expires.get(key)
        if expires_at is None:
            return -1
        return max(int((expires_at - _now()) * 1000), 0)

    async def get(self, key: str | bytes) -> str | None:
        await self._check_failure()
        key_norm = _normalize_key(key)
        self._purge_expired(key_norm)
        return self._store.get(key_norm)

    async def set(
        self,
        key: str | bytes,
        value: Any,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        await self._check_failure()
        return self._set(_normalize_key(key), value, ex=ex, px=px, nx=nx)

    async def setex(self, key: str | bytes, time_seconds: int, value: Any) -> bool:
        await self._check_failure()
        return self._set(_normalize_key(key), value, ex=time_seconds)

    def _set(
        self,
        key: str,
        value: Any,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        if nx:
            self._purge_expired(key)
            if key in self._store:
                return False
        self._store[key] = _normalize_value(value)
        if ex is not None:
            self._expires[key] = _now() + int(ex)
        elif px is not None:
            self._expires[key] = _now() + (int(px) / 1000)
        else:
            self._expires.pop(key, None)
        return True

    async def delete(self, *keys: str | bytes) -> int:
        await self._check_failure()
        deleted = 0
        for key in keys:
            key_norm = _normalize_key(key)
            self._purge_expired(key_norm)
            if key_norm in self._store:
                self._store.pop(key_norm, None)
                self._expires.pop(key_norm, None)
                deleted += 1
        return deleted

    async def exists(self, *keys: str | bytes) -> int:
        """How many keys exist, a repeated key counted each time - as Redis does."""
        await self._check_failure()
        present = 0
        for key in keys:
            key_norm = _normalize_key(key)
            self._purge_expired(key_norm)
            present += int(key_norm in self._store)
        return present

    async def expire(self, key: str | bytes, seconds: int, *, nx: bool = False) -> bool:
        await self._check_failure()
        key_norm = _normalize_key(key)
        self._purge_expired(key_norm)
        if key_norm not in self._store:
            return False
        if nx and key_norm in self._expires:
            return False
        self._expires[key_norm] = _now() + int(seconds)
        return True

    async def incr(self, key: str | bytes) -> int:
        await self._check_failure()
        key_norm = _normalize_key(key)
        self._purge_expired(key_norm)
        # Writes the store directly: set() would drop the TTL, but Redis INCR
        # preserves it.
        value = int(self._store.get(key_norm, "0")) + 1
        self._store[key_norm] = str(value)
        return value

    async def ttl(self, key: str | bytes) -> int:
        await self._check_failure()
        key_norm = _normalize_key(key)
        self._purge_expired(key_norm)
        if key_norm not in self._store:
            return -2
        expires_at = self._expires.get(key_norm)
        if expires_at is None:
            return -1
        # round(), not int(): truncation would report one second short whenever
        # a fraction of a millisecond elapses between set() and this call.
        return max(0, round(expires_at - _now()))

    async def pttl(self, key: str | bytes) -> int:
        await self._check_failure()
        return self._pttl(_normalize_key(key))

    async def scan(
        self,
        cursor: int = 0,
        match: str | None = None,
        count: int | None = None,
    ) -> tuple[int, list[str]]:
        await self._check_failure()
        for key in list(self._store.keys()):
            self._purge_expired(key)

        keys = list(self._store.keys())
        if match:
            keys = [key for key in keys if fnmatch.fnmatch(key, match)]
        if count is not None:
            keys = keys[:count]
        return 0, keys

    async def script_load(self, script: str) -> str:
        await self._check_failure()
        sha = hashlib.sha1(script.encode("utf-8")).hexdigest()
        self._scripts[sha] = script
        return sha

    async def evalsha(self, sha: str, numkeys: int, *keys_and_args: Any) -> int:
        await self._check_failure()
        if sha not in self._scripts:
            raise redis_exc.NoScriptError(
                "NOSCRIPT No matching script. Please use EVAL."
            )
        if self._scripts[sha].strip() != RATE_LIMIT_SCRIPT.strip():
            raise NotImplementedError("Script not supported in fake Redis.")
        key = _normalize_key(keys_and_args[0])
        self.evalsha_keys.append(key)
        return self._eval_rate_limit(
            key, int(keys_and_args[numkeys]), int(keys_and_args[numkeys + 1])
        )

    # Mirrors src/core/limiter/script.py; tests/integration/src/core/limiter pins
    # the real script to the same answers. Script bodies are synchronous: an
    # await between the read that decides and the write that acts would let two
    # callers interleave where real Redis, which runs a script as one unit,
    # cannot.
    def _eval_rate_limit(self, key: str, limit: int, window_ms: int) -> int:
        current = int(self._read(key) or 0)
        if current > 0:
            if current + 1 > limit:
                return max(self._pttl(key), 1)
            self._store[key] = str(current + 1)
            return 0
        if self.out_of_memory:
            raise redis_exc.OutOfMemoryError(
                "OOM command not allowed when used memory > 'maxmemory'."
            )
        self._store[key] = "1"
        self._expire_ms(key, window_ms)
        return 0

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
    ) -> Any:
        await self._check_failure()
        raise NotImplementedError("Script not supported in fake Redis.")

    async def info(self, section: str | None = None) -> dict[str, int]:
        """No memory cap, like a Redis started without `maxmemory`."""
        await self._check_failure()
        return {"used_memory": 0, "maxmemory": 0}

    async def time(self) -> tuple[int, int]:
        await self._check_failure()
        now = self.wall_clock()
        return int(now), int((now % 1) * 1_000_000)

    async def ping(self) -> bool:
        await self._check_failure()
        return True

    def keys_snapshot(self) -> list[str]:
        """All keys currently held. For tests asserting a value never leaked into
        a key, not a Redis API."""
        return list(self._store)

    async def aclose(self) -> None:
        self.closed = True
