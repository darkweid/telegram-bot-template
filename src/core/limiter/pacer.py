import asyncio
from collections.abc import Awaitable, Callable
import time


class Pacer:
    """Spaces calls `1 / rate_per_second` seconds apart, within one process.

    A local pace, not a shared limit: a broadcast runs in one worker at a
    time, so it meets the Bot API's global limit without a Redis round trip
    per message.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._interval = 1 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_at: float | None = None

    async def wait(self) -> None:
        """Return when the next call may go; the first one goes at once."""
        now = self._clock()
        if self._next_at is not None and now < self._next_at:
            await self._sleep(self._next_at - now)
            now = self._next_at
        self._next_at = now + self._interval
