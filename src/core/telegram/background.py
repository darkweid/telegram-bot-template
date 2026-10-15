"""Work an update starts and does not wait for: a model call, an agent run.

The handler answers at once and lets go of the user's event-isolation lock;
the work runs in a task of its own until it is done, or until a stopping bot
cancels it (src/main/runtime.py).
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext

from aiogram.exceptions import TelegramForbiddenError
from dishka import AsyncContainer
import sentry_sdk

from loggers import get_logger
from src.core.errors.exceptions import CoreException
from src.core.errors.reporting import report_core_exception
from src.core.errors.scrubbed import reportable
from src.core.telegram.alerts import Alert, DeveloperAlerter
from src.core.telegram.context import sentry_scope_for
from src.core.telegram.session import update_sleep_budget
from src.core.update_context import get_update_context

logger = get_logger(__name__)

type BackgroundWork = Callable[[AsyncContainer], Awaitable[None]]


class BackgroundRunner:
    """Runs background work and reports what it raises; the user hears of a
    failure from the work itself (ProcessingStatus), never from the runner.

    `work` gets the root container and opens a scope of its own for each
    transaction: the update's scope closes with the update, long before the
    work ends.
    """

    def __init__(self, container: AsyncContainer, alerter: DeveloperAlerter) -> None:
        self._container = container
        self._alerter = alerter
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def tasks(self) -> frozenset[asyncio.Task[None]]:
        return frozenset(self._tasks)

    def start(self, name: str, work: BackgroundWork) -> None:
        """Start `work` in a task that copies the caller's context: the
        update's ids, its locale and its I18n go with it.

        Raises RuntimeError once `drain` has begun: a stopping bot takes no
        updates, so a start then is a bug.
        """
        if self._closed:
            raise RuntimeError(f"background work {name} started after drain")
        task = asyncio.create_task(self._run(name, work), name=f"background {name}")
        # The loop keeps only a weak reference: an unreferenced task can be
        # collected mid-run.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, grace_seconds: float) -> None:
        """Refuse new work, give the running work `grace_seconds`, then
        cancel the rest and wait for it without a bound: work that is
        cancelled records its call and edits its status under bounds of its
        own."""
        self._closed = True
        pending = set(self._tasks)
        if not pending:
            return
        if grace_seconds > 0:
            logger.info(
                "Waiting up to %ss for %d background task(s)",
                grace_seconds,
                len(pending),
            )
            _, pending = await asyncio.wait(pending, timeout=grace_seconds)
            if not pending:
                return
        logger.warning(
            "Cancelling %d background task(s) still running after %ss",
            len(pending),
            max(grace_seconds, 0),
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _run(self, name: str, work: BackgroundWork) -> None:
        context = get_update_context()
        # The copied ContextVar points at the update's own budget, which the
        # update may have spent: the work gets a fresh one.
        with (
            update_sleep_budget(),
            sentry_scope_for(context) if context is not None else nullcontext(),
        ):
            try:
                await work(self._container)
            except asyncio.CancelledError:
                logger.info("Background work %s cancelled", name)
                raise
            except TelegramForbiddenError as error:
                # ForbiddenMiddleware has recorded the user as unreachable;
                # there is nobody to tell.
                logger.info("Background work %s not delivered: %s", name, error.message)
            except CoreException as error:
                report_core_exception(logger, error)
            except Exception as error:
                safe = reportable(error)
                logger.error(
                    "Background work %s failed: %s",
                    name,
                    type(error).__name__,
                    exc_info=safe,
                )
                event_id = sentry_sdk.capture_exception(safe)
                self._alerter.alert_error_soon(
                    Alert(
                        safe,
                        origin=f"background {name}",
                        user_id=context.user_id if context else None,
                        sentry_event_id=event_id,
                    )
                )
