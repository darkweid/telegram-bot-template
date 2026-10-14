from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import pytest

from src.core.events.registry import event_subscribers, set_event_subscribers
from src.core.events.schemas import Actor, ActorType, DomainEvent


@dataclass(frozen=True, slots=True)
class Pinged(DomainEvent):
    code: ClassVar[str] = "test.pinged"
    target_id: int


@pytest.fixture(autouse=True)
def _restore_registry() -> Iterator[None]:
    saved = event_subscribers()
    yield
    set_event_subscribers(saved)


def test_actors_say_who_acted() -> None:
    assert Actor.telegram_user(42) == Actor(type=ActorType.TELEGRAM_USER, id=42)
    assert Actor.system() == Actor(type=ActorType.SYSTEM, id=None)


def test_the_registry_is_replaced_not_merged() -> None:
    """build_container sets it on every container; tests build many, and a
    merge would run each subscriber once per container built so far."""

    async def subscriber(*_: object) -> None:
        return None

    set_event_subscribers({Pinged: [subscriber]})
    set_event_subscribers({Pinged: [subscriber]})
    assert event_subscribers() == {Pinged: (subscriber,)}
