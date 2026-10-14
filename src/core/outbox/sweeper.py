"""Background tasks that keep the outbox moving and small."""

from datetime import timedelta

from dishka import FromDishka
import sentry_sdk
from taskiq import AsyncBroker

from loggers import get_logger
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.outbox.models import OutboxMessage
from src.core.outbox.publisher import (
    UnregisteredTaskError,
    broker_error,
    is_broker_unreachable,
    kick_stored,
)
from src.core.outbox.repositories import OutboxRepository
from src.core.utils.datetime_utils import get_utc_now

logger = get_logger(__name__)

SWEEPER_BATCH_SIZE = 100
MAX_PUBLISH_ATTEMPTS = 10
PUBLISHED_RETENTION_DAYS = 7
# Rows per DELETE of the purge: each statement stays far inside the
# connection's statement timeout however large the backlog has grown.
PURGE_BATCH_SIZE = 1000
# How long a fresh row belongs to its own after-commit publish before the
# sweeper may take it. Far longer than a send plus one UPDATE ever takes, and
# short enough that a publish which did fail is retried within a sweep or two.
SWEEPER_GRACE = timedelta(seconds=60)
_outbox = OutboxRepository()


def _report_failed(message: OutboxMessage) -> None:
    # A row gave up and needs a person. CRITICAL is what the logging
    # integration turns into a Sentry event; the scope tags it.
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("outbox_message_id", str(message.id))
        scope.set_tag("task_name", message.task_name)
        logger.critical(
            "Outbox message %s (%s) moved to FAILED", message.id, message.task_name
        )


async def sweep_outbox(
    uow: FromDishka[SQLAlchemyUnitOfWork], broker: FromDishka[AsyncBroker]
) -> str:
    """Send pending outbox rows whose after-commit publish did not happen."""
    published = failed = 0
    async with uow:
        batch = await _outbox.get_batch_for_publish(
            uow.session, limit=SWEEPER_BATCH_SIZE, min_age=SWEEPER_GRACE
        )
        for message in batch:
            try:
                await kick_stored(
                    broker, message.id, message.task_name, message.args, message.kwargs
                )
            except UnregisteredTaskError:
                await _outbox.mark_publish_failure(
                    uow.session, message.id, "task is not registered", final=True
                )
                _report_failed(message)
                failed += 1
                continue
            except Exception as error:
                cause = broker_error(error)
                if is_broker_unreachable(error):
                    await _outbox.mark_publish_failure(
                        uow.session,
                        message.id,
                        type(cause).__name__,
                        final=False,
                        count_attempt=False,
                    )
                    failed += 1
                    logger.warning(
                        "Outbox sweep stopped, the broker is unreachable (%s)",
                        type(cause).__name__,
                    )
                    # Every further send would fail the same way, a hanging one
                    # only after SEND_TIMEOUT_SECONDS, while this transaction
                    # holds the batch's row locks; the next sweep takes it.
                    break
                # Any other refusal is the message's own: count it, give up
                # after MAX_PUBLISH_ATTEMPTS.
                final = message.attempts + 1 >= MAX_PUBLISH_ATTEMPTS
                await _outbox.mark_publish_failure(
                    uow.session, message.id, type(cause).__name__, final=final
                )
                if final:
                    _report_failed(message)
                failed += 1
                continue
            await _outbox.mark_published(uow.session, message.id)
            published += 1
        await uow.commit()
    if published or failed:
        logger.info("Outbox sweep: published %d, failed %d", published, failed)
    return f"Published {published}, failed {failed}."


async def purge_outbox(uow: FromDishka[SQLAlchemyUnitOfWork]) -> str:
    """Delete published rows older than the retention window, in batches of
    PURGE_BATCH_SIZE."""
    cutoff = get_utc_now() - timedelta(days=PUBLISHED_RETENTION_DAYS)
    deleted = 0
    async with uow:
        while True:
            batch = await _outbox.purge_published(
                uow.session, cutoff, limit=PURGE_BATCH_SIZE
            )
            deleted += batch
            if batch < PURGE_BATCH_SIZE:
                break
        await uow.commit()
    return f"Deleted {deleted} published outbox messages."
