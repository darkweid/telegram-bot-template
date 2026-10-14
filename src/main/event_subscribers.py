"""Which subscribers each domain event runs, in order. Filled here because
only the composition root may import every domain."""

from src.core.events.registry import EventSubscribers

EVENT_SUBSCRIBERS: EventSubscribers = {}
