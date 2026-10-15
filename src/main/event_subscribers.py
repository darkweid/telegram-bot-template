"""Which subscribers each domain event runs, in order. Filled here because
only the composition root may import every domain."""

from src.core.events.registry import EventSubscribers
from src.note.events import NoteCreated
from src.note.subscribers import enqueue_note_created
from src.user.events import UserRegistered
from src.user.subscribers import enqueue_new_user_notice

EVENT_SUBSCRIBERS: EventSubscribers = {
    UserRegistered: (enqueue_new_user_notice,),
    NoteCreated: (enqueue_note_created,),
}
