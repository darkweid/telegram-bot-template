from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.schemas import PublishedEvent
from src.note.events import NoteCreated
from src.note.tasks import NOTE_CREATED


async def enqueue_note_created(
    uow: SQLAlchemyUnitOfWork, published: PublishedEvent
) -> None:
    """Queue the note's task in the note's own transaction."""
    event = published.event
    # The registry is typed by DomainEvent; this fails only when
    # EVENT_SUBSCRIBERS maps the subscriber to the wrong event.
    if not isinstance(event, NoteCreated):
        raise TypeError(f"Subscribed to NoteCreated, got {type(event).__name__}")
    # The id travels as a string: the arguments are stored and sent as JSON.
    await uow.enqueue_task(
        NOTE_CREATED, note_id=str(event.note_id), user_id=event.user_id
    )
