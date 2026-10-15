from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING

from src.core.events.schemas import DomainEvent, PublishedEvent

if TYPE_CHECKING:
    # The unit of work imports this module to find subscribers.
    from src.core.database.uow import SQLAlchemyUnitOfWork

EventSubscriber = Callable[["SQLAlchemyUnitOfWork", PublishedEvent], Awaitable[None]]
EventSubscribers = Mapping[type[DomainEvent], Sequence[EventSubscriber]]

_subscribers: EventSubscribers = MappingProxyType({})


def set_event_subscribers(subscribers: EventSubscribers) -> None:
    """Replace the registry. The composition root calls it with
    EVENT_SUBSCRIBERS whenever it builds a container; replacing rather than
    merging keeps a second container from running every subscriber twice."""
    global _subscribers
    _subscribers = MappingProxyType(
        {event: tuple(handlers) for event, handlers in subscribers.items()}
    )


def event_subscribers() -> EventSubscribers:
    return _subscribers
