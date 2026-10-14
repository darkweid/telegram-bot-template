from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any, Self
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
import uuid6

from loggers import get_logger
from src.core.errors.scrubbed import reportable
from src.core.events.registry import event_subscribers
from src.core.events.schemas import Actor, DomainEvent, PublishedEvent
from src.core.outbox.interface import TaskPublisher
from src.core.outbox.payload import ensure_json_payload
from src.core.outbox.repositories import OutboxRepository
from src.core.utils.datetime_utils import get_utc_now

logger = get_logger(__name__)

AfterCommitHook = Callable[[], Awaitable[None]]


class SQLAlchemyUnitOfWork:
    """
    Single-use transaction boundary over an AsyncSession.

    Each use case of an update gets a unit of work of its own over the
    update's one session, so their transactions run there one after another.
    A unit of work is always the top-level transaction: entering one on a
    session that is already in a transaction raises. Commit is strictly
    explicit: leaving the context without calling commit() rolls the
    transaction back, whether the block raised or returned normally. Any later
    `uow.*` call raises RuntimeError.

    `publish` runs domain event subscribers in the transaction; `enqueue_task`
    stores a background task as an outbox row and sends it after commit.
    """

    def __init__(self, session: AsyncSession, publisher: TaskPublisher | None = None):
        self._session = session
        self._publisher = publisher
        self._outbox = OutboxRepository()
        self._is_completed = False
        # This unit's own entry. The session flag below is shared by every
        # unit on the session and only tells that some unit is open.
        self._entered = False
        self._after_commit_hooks: list[AfterCommitHook] = []

    async def __aenter__(self) -> Self:
        """
        Begin the session's transaction.

        Raises RuntimeError when the session is already in one - an earlier
        read on the shared session autobegins a transaction, and so does an
        enclosing unit of work. A SAVEPOINT there would make `commit()` commit
        that outer transaction too, work this unit never saw included. Nothing
        is touched on refusal: the session stays in its transaction.

        Also raises once this unit has been committed or rolled back: it is
        single-use, and a second entry would open a transaction nothing ends.
        Another transaction on the same session takes another unit of work.
        """
        self._ensure_not_completed()
        if self._session.in_transaction():
            raise RuntimeError(
                "UnitOfWork entered while the session is already in a "
                "transaction. Read through `detached_read` or move the read "
                "inside the UnitOfWork; a UnitOfWork is never nested."
            )
        await self._session.begin()
        self._session.info["uow_active"] = True
        self._entered = True
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """
        Roll back on any exit without a prior commit().

        Without this, a clean exit would leave the transaction open on the
        session until it closes, and its outcome would depend on what the
        caller did next.
        """
        try:
            if not self._is_completed:
                if exc_type is None and self._has_pending_changes():
                    logger.warning(
                        "UnitOfWork exited cleanly without commit() while "
                        "changes were pending; rolling back."
                    )
                await self.rollback()
        finally:
            self._entered = False
            self._session.info.pop("uow_active", None)

    def _has_pending_changes(self) -> bool:
        # Best-effort forgotten-commit detection: already-flushed changes are
        # not visible in these collections, so silence proves nothing.
        return bool(self._session.dirty or self._session.new or self._session.deleted)

    def _ensure_not_completed(self) -> None:
        if self._is_completed:
            raise RuntimeError("This unit of work has already been completed")

    def add_after_commit_hook(self, hook: AfterCommitHook) -> None:
        """Register a coroutine to run once after a successful commit."""
        self._ensure_not_completed()
        self._after_commit_hooks.append(hook)

    async def commit(self) -> None:
        self._ensure_not_completed()
        await self._session.commit()
        self._is_completed = True
        await self._run_after_commit_hooks()

    async def rollback(self) -> None:
        self._ensure_not_completed()
        await self._session.rollback()
        self._is_completed = True
        self._after_commit_hooks = []

    async def flush(self) -> None:
        self._ensure_not_completed()
        await self._session.flush()

    async def refresh(
        self,
        instance: Any,
        attribute_names: Sequence[str] | None = None,
        with_for_update: Any | None = None,
    ) -> None:
        self._ensure_not_completed()
        await self._session.refresh(
            instance,
            attribute_names=attribute_names,
            with_for_update=with_for_update,
        )

    async def publish(self, actor: Actor, event: DomainEvent) -> None:
        """Run the event's subscribers now, in this transaction: whatever they
        write commits or rolls back with the action that caused the event."""
        self._ensure_active()
        published = PublishedEvent(event=event, actor=actor, occurred_at=get_utc_now())
        for subscriber in event_subscribers().get(type(event), ()):
            await subscriber(self, published)

    async def enqueue_task(self, task_name: str, *args: Any, **kwargs: Any) -> UUID:
        """Store a background task in the outbox, in this transaction.

        After commit the publisher sends it; when there is no publisher, or the
        send fails, the outbox sweeper sends it within a couple of minutes.
        Arguments must survive a JSON round-trip unchanged: ids and plain
        values. The row id is the task id the worker deduplicates on.
        """
        self._ensure_active()
        ensure_json_payload(task_name, args, kwargs)
        # Generated here, not by the column default: the after-commit hook
        # needs it before any flush.
        message_id = uuid6.uuid7()
        await self._outbox.create(
            self._session,
            {
                "id": message_id,
                "task_name": task_name,
                "args": list(args),
                "kwargs": kwargs,
            },
        )
        if self._publisher is not None:
            self.add_after_commit_hook(
                partial(
                    self._publisher.publish, message_id, task_name, list(args), kwargs
                )
            )
        return message_id

    def _ensure_active(self) -> None:
        # Checked on this unit, not on the session: another unit open on the
        # same session would take the writes into its transaction, while this
        # unit's after-commit hooks never run.
        self._ensure_not_completed()
        if not self._entered:
            raise RuntimeError("Call this inside `async with uow:`")

    async def _run_after_commit_hooks(self) -> None:
        hooks, self._after_commit_hooks = self._after_commit_hooks, []
        for hook in hooks:
            # Best-effort by design: the data is committed, so a failed hook
            # must not turn the update into an error. This is the one
            # sanctioned exception swallow. A hook may write (the outbox
            # publisher does), so its error is scrubbed like any other.
            try:
                await hook()
            except Exception as error:
                logger.error("After-commit hook failed", exc_info=reportable(error))

    @property
    def completed(self) -> bool:
        """True once the unit of work has been committed or rolled back."""
        return self._is_completed

    @property
    def session(self) -> AsyncSession:
        return self._session
