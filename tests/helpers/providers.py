from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.utils.i18n import I18n
from dishka import AsyncContainer, Provider, Scope, provide

from src.core.cache.interface import Cache
from src.core.cache.memory_cache import InMemoryCache
from src.core.cache.serializer import JsonSerializer
from src.core.limiter.limiter import RateLimiter
from src.core.outbox.interface import TaskPublisher
from src.core.redis.degradation import RedisDegradationReporter
from src.core.telegram.alerts import AlertLimiter, AlertSession
from src.core.telegram.middlewares.throttling import ThrottleLimiter
from src.core.telegram.session import UnreachableUserRecorder
from src.main.config import Config
from src.main.di import build_container
from tests.fakes.redis import InMemoryRedis
from tests.fakes.telegram import FakeTelegramSession


class RecordingUnreachableRecorder:
    def __init__(self) -> None:
        self.unreachable: list[int] = []

    async def record_unreachable(self, user_id: int) -> None:
        self.unreachable.append(user_id)


class RecordingTaskPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, list[Any], dict[str, Any]]] = []

    async def publish(
        self, message_id: UUID, task_name: str, args: list[Any], kwargs: dict[str, Any]
    ) -> None:
        self.published.append((task_name, args, kwargs))


class SessionOnly(Provider):
    """The network and the catalogs, nothing else: for tests that check a
    production provider which TestOverrides replaces with a fake."""

    __test__ = False
    scope = Scope.APP

    def __init__(
        self,
        session: FakeTelegramSession,
        i18n: I18n,
        *,
        alert_session: FakeTelegramSession | None = None,
    ) -> None:
        super().__init__()
        self._session = session
        # Its own fake: the alert Bot registers its middleware on its session,
        # and on the handlers' fake it would wrap every call twice.
        self.alert_session = alert_session or FakeTelegramSession()
        self._i18n = i18n

    @provide(override=True)
    async def session(self) -> AsyncIterator[BaseSession]:
        # Closed by the container, like the production session.
        yield self._session
        await self._session.close()

    @provide(override=True)
    def i18n(self) -> I18n:
        return self._i18n

    @provide(override=True)
    async def alert_telegram_session(self) -> AsyncIterator[AlertSession]:
        yield AlertSession(self.alert_session)
        await self.alert_session.close()


class TestOverrides(SessionOnly):
    """What every in-process test replaces: the network, Redis-backed FSM, and
    catalogs compiled from the working tree. The database engine and the Redis
    client stay real but are never used by unit tests: both connect lazily."""

    __test__ = False  # not a test class, despite the name

    def __init__(
        self,
        session: FakeTelegramSession,
        i18n: I18n,
        *,
        alert_session: FakeTelegramSession | None = None,
    ) -> None:
        super().__init__(session, i18n, alert_session=alert_session)
        self.recorder = RecordingUnreachableRecorder()
        self.publisher = RecordingTaskPublisher()

    @provide(override=True)
    def fsm_storage(self) -> BaseStorage:
        return MemoryStorage()

    @provide(override=True)
    def event_isolation(self) -> BaseEventIsolation:
        return SimpleEventIsolation()

    @provide(override=True)
    def cache(self) -> Cache:
        # In process and per container: tests never share cached users.
        return InMemoryCache(JsonSerializer(), default_ttl=60, version_ttl=600)

    @provide(override=True)
    def unreachable_recorder(self) -> UnreachableUserRecorder:
        return self.recorder

    @provide(override=True)
    def throttle_limiter(self) -> ThrottleLimiter:
        # A fake per container: tests never share counters or reach Redis.
        return ThrottleLimiter(
            RateLimiter(
                InMemoryRedis(),
                prefix="throttle",
                reporter=RedisDegradationReporter("Throttling"),
            )
        )

    @provide(override=True)
    def task_publisher(self) -> TaskPublisher:
        # Unit tests record what would be sent; the in-memory broker would run
        # the task inline, inside the test's update.
        return self.publisher

    @provide(override=True)
    def alert_limiter(self) -> AlertLimiter:
        # A fake per container: a cooldown never carries over between tests.
        return AlertLimiter(
            RateLimiter(
                InMemoryRedis(),
                prefix="alerts",
                reporter=RedisDegradationReporter("Alerts"),
            )
        )


def build_test_container(
    config: Config,
    session: FakeTelegramSession,
    i18n: I18n,
    *providers: Provider,
    alert_session: FakeTelegramSession | None = None,
) -> AsyncContainer:
    return build_container(
        config, TestOverrides(session, i18n, alert_session=alert_session), *providers
    )
