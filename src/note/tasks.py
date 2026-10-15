from loggers import get_logger
from src.core.tasks.registry import TaskSpec

logger = get_logger(__name__)

NOTE_CREATED = "note.note_created"


async def note_created(note_id: str, user_id: int) -> None:
    """Runs on the worker after a note is committed.

    Shows the path an event takes (use case, subscriber, outbox, broker,
    worker); a real bot puts its follow-up work here.
    """
    logger.info("Note %s created by user %s", note_id, user_id)


NOTE_TASKS: tuple[TaskSpec, ...] = (TaskSpec(NOTE_CREATED, note_created),)
