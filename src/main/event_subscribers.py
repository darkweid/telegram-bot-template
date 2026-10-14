"""Which subscribers each domain event runs, in order. Filled here because
only the composition root may import every domain."""

from src.core.events.registry import EventSubscribers
from src.user.events import UserRegistered
from src.user.subscribers import enqueue_new_user_notice

EVENT_SUBSCRIBERS: EventSubscribers = {
    UserRegistered: (enqueue_new_user_notice,),
}
