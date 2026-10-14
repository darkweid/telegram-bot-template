from collections.abc import AsyncIterator

from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.utils.i18n import I18n
from dishka import AsyncContainer, Provider, Scope, provide

from src.core.telegram.session import UnreachableUserRecorder
from src.main.config import Config
from src.main.di import build_container
from tests.fakes.telegram import FakeTelegramSession


class RecordingUnreachableRecorder:
    def __init__(self) -> None:
        self.unreachable: list[int] = []

    async def record_unreachable(self, user_id: int) -> None:
        self.unreachable.append(user_id)


class TestOverrides(Provider):
    """What every in-process test replaces: the network, Redis-backed FSM, and
    catalogs compiled from the working tree. The database engine and the Redis
    client stay real but are never used by unit tests: both connect lazily."""

    __test__ = False  # not a test class, despite the name
    scope = Scope.APP

    def __init__(self, session: FakeTelegramSession, i18n: I18n) -> None:
        super().__init__()
        self._session = session
        self._i18n = i18n
        self.recorder = RecordingUnreachableRecorder()

    @provide(override=True)
    async def session(self) -> AsyncIterator[BaseSession]:
        # Closed by the container, like the production session.
        yield self._session
        await self._session.close()

    @provide(override=True)
    def fsm_storage(self) -> BaseStorage:
        return MemoryStorage()

    @provide(override=True)
    def event_isolation(self) -> BaseEventIsolation:
        return SimpleEventIsolation()

    @provide(override=True)
    def i18n(self) -> I18n:
        return self._i18n

    @provide(override=True)
    def unreachable_recorder(self) -> UnreachableUserRecorder:
        return self.recorder


def build_test_container(
    config: Config, session: FakeTelegramSession, i18n: I18n, *providers: Provider
) -> AsyncContainer:
    return build_container(config, TestOverrides(session, i18n), *providers)
