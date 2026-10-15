from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.schemas import PublishedEvent
from src.user.events import UserRegistered
from src.user.tasks import NOTIFY_ADMINS_OF_NEW_USER


async def enqueue_new_user_notice(
    uow: SQLAlchemyUnitOfWork, published: PublishedEvent
) -> None:
    """Queue the admin notice in the registration's own transaction."""
    event = published.event
    # The registry is typed by DomainEvent; this fails only when
    # EVENT_SUBSCRIBERS maps the subscriber to the wrong event.
    if not isinstance(event, UserRegistered):
        raise TypeError(f"Subscribed to UserRegistered, got {type(event).__name__}")
    await uow.enqueue_task(NOTIFY_ADMINS_OF_NEW_USER, user_id=event.user_id)
