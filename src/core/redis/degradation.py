from collections.abc import Callable
from threading import Lock
import time
from typing import Protocol

from redis.exceptions import RedisError
import sentry_sdk

from loggers import get_logger

logger = get_logger(__name__)

DEFAULT_COOLDOWN_MS = 5 * 60_000
DEFAULT_RECOVERY_QUIET_MS = 60_000


class DegradationNotifier(Protocol):
    """Hears what the reporter sends to Sentry. Synchronous: the reporter is
    called from synchronous code, after it has released its lock; an
    implementation that needs to await schedules its own task. An exception
    from `notify` is logged by the reporter and never reaches the caller."""

    def notify(self, message: str) -> None: ...


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


class RedisDegradationReporter:
    """
    Report Redis degradation to Sentry with a cooldown and a paired recovery notice.

    Degraded infrastructure that does not raise is the one case where a manual
    capture_message is allowed; the cooldown keeps a flapping Redis from
    flooding Sentry, and the recovery message closes the incident with its
    duration.

    A success closes the incident only once no failure has been seen for
    `recovery_quiet_ms`. A Redis at the edge of maxmemory refuses one write and
    accepts the next; closing on the first success also resets the cooldown, so
    every refusal after it would file a fresh report at once: a pair per flap.

    A notifier, when given, hears the same two messages.
    """

    def __init__(
        self,
        component: str,
        *,
        cooldown_ms: int = DEFAULT_COOLDOWN_MS,
        recovery_quiet_ms: int = DEFAULT_RECOVERY_QUIET_MS,
        clock: Callable[[], int] = _monotonic_ms,
        notifier: DegradationNotifier | None = None,
    ) -> None:
        self._component = component
        self._cooldown_ms = cooldown_ms
        self._recovery_quiet_ms = recovery_quiet_ms
        self._clock = clock
        self._notifier = notifier
        self._lock = Lock()
        self._degraded_since_ms: int | None = None
        self._last_failure_ms: int | None = None
        self._failure_count = 0
        self._last_report_ms: int | None = None

    def report_degraded(self, error: RedisError) -> None:
        with self._lock:
            # Read under the lock, or a stale timestamp could overwrite a newer one.
            now_ms = self._clock()
            self._last_failure_ms = now_ms
            self._failure_count += 1
            if self._degraded_since_ms is None:
                self._degraded_since_ms = now_ms
            elif (
                self._last_report_ms is not None
                and now_ms - self._last_report_ms < self._cooldown_ms
            ):
                return
            self._last_report_ms = now_ms

        # str(error) is safe here: callers pass only FAIL_OPEN_ERRORS, redis-py
        # transport and capacity errors whose text names the server, never a
        # key or a value. The message goes to Sentry and to developers' chats,
        # which is why the parameter is a RedisError and not any Exception.
        message = (
            f"[{self._component}] Redis is degraded. "
            f"Error: {type(error).__name__}: {error}"
        )
        sentry_sdk.capture_message(message, level="error")
        self._notify(message)

    def report_recovered(self) -> None:
        with self._lock:
            now_ms = self._clock()
            degraded_since_ms = self._degraded_since_ms
            last_failure_ms = self._last_failure_ms
            if degraded_since_ms is None or last_failure_ms is None:
                return
            if now_ms - last_failure_ms < self._recovery_quiet_ms:
                return
            failure_count = self._failure_count
            self._degraded_since_ms = None
            self._last_failure_ms = None
            self._failure_count = 0
            self._last_report_ms = None

        # Reported a quiet window after the last failure, so the message places
        # the incident relative to itself instead of to its own timestamp.
        message = (
            f"[{self._component}] Redis recovered after {failure_count} failure(s): "
            f"the first {now_ms - degraded_since_ms}ms ago, "
            f"the last {now_ms - last_failure_ms}ms ago."
        )
        sentry_sdk.capture_message(message, level="info")
        self._notify(message)

    def _notify(self, message: str) -> None:
        if self._notifier is None:
            return
        # The callers are fail-open paths: an alert sink that is down during a
        # Redis outage must not turn the outage into failed updates, and the
        # incident state is already committed, so a retry would stay muted.
        try:
            self._notifier.notify(message)
        except Exception as error:
            logger.error(
                "[%s] Degradation notifier failed: %s",
                self._component,
                type(error).__name__,
            )
