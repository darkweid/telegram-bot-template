from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide
from dishka.exceptions import NoFactoryError
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from taskiq import AsyncBroker, InMemoryBroker

from src.core.cache.interface import Cache
from src.core.cache.redis_cache import RedisCache
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.registry import event_subscribers, set_event_subscribers
from src.core.events.schemas import DomainEvent
from src.core.http.user_agent import UserAgent
from src.core.telegram.session import ForbiddenMiddleware, RetryAfterMiddleware
from src.main import di as di_module
from src.main.config import BotConfig, Config
from src.main.di import build_container
from src.system.repositories import SystemRepository
from src.system.services import HealthService
from tests.fakes.db import FakeAsyncSession
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.providers import SessionOnly, TestOverrides, build_test_container


async def test_production_container_resolves_every_app_dependency(
    settings: Config,
) -> None:
    container = build_container(settings)
    try:
        assert await container.get(Config) is settings
        assert await container.get(BotConfig) is settings.bot
        for dependency in (
            AsyncEngine,
            Redis,
            BaseStorage,
            BaseEventIsolation,
            BaseSession,
            Bot,
        ):
            await container.get(dependency)
    finally:
        await container.close()


async def test_bot_sends_html_without_link_previews(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    container = build_test_container(settings, session, i18n)
    try:
        bot = await container.get(Bot)
        assert bot.session is session
        assert bot.default.parse_mode == ParseMode.HTML
        assert bot.default.link_preview is not None
        assert bot.default.link_preview.is_disabled is True
        assert isinstance(await container.get(BaseStorage), MemoryStorage)
        assert await container.get(I18n) is i18n
    finally:
        await container.close()


async def test_session_middlewares_are_registered_retry_first(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    container = build_test_container(settings, session, i18n)
    try:
        await container.get(Bot)
        assert [type(m) for m in session.middleware] == [
            RetryAfterMiddleware,
            ForbiddenMiddleware,
        ]
    finally:
        await container.close()


async def test_each_unit_of_work_of_a_request_runs_on_its_one_session(
    settings: Config, i18n: I18n
) -> None:
    container = build_test_container(settings, FakeTelegramSession(), i18n)
    try:
        async with container() as first:
            session = await first.get(AsyncSession)
            uow = await first.get(SQLAlchemyUnitOfWork)
            other = await first.get(SQLAlchemyUnitOfWork)
            assert other is not uow
            assert uow.session is session
            assert other.session is session
        async with container() as second:
            assert await second.get(SQLAlchemyUnitOfWork) is not uow
    finally:
        await container.close()


async def test_building_a_container_installs_the_event_subscribers(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def subscriber(*_: object) -> None:
        return None

    saved = event_subscribers()
    marker = {DomainEvent: [subscriber]}
    monkeypatch.setattr(di_module, "EVENT_SUBSCRIBERS", marker)
    container = build_test_container(settings, FakeTelegramSession(), i18n)
    try:
        assert event_subscribers() == {DomainEvent: (subscriber,)}
    finally:
        await container.close()
        set_event_subscribers(saved)


async def test_closing_the_container_releases_the_bot_session(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    closed: list[bool] = []
    original_close = session.close

    async def tracking_close() -> None:
        closed.append(True)
        await original_close()

    session.close = tracking_close
    container = build_test_container(settings, session, i18n)
    await container.get(Bot)
    await container.close()
    assert closed == [True]


def spy(monkeypatch: pytest.MonkeyPatch, owner: type, name: str) -> list[object]:
    """Record the instance of every call to `owner.name`, then run the original."""
    original: Callable[..., Awaitable[Any]] = getattr(owner, name)
    calls: list[object] = []

    async def wrapper(self: object, *args: Any, **kwargs: Any) -> Any:
        calls.append(self)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)
    return calls


async def test_closing_the_container_releases_each_resource_once(
    settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposed = spy(monkeypatch, AsyncEngine, "dispose")
    redis_closed = spy(monkeypatch, Redis, "aclose")
    storage_closed = spy(monkeypatch, RedisStorage, "close")
    container = build_container(settings)
    engine = await container.get(AsyncEngine)
    client = await container.get(Redis)
    storage = await container.get(BaseStorage)
    assert disposed == [] and redis_closed == [] and storage_closed == []

    await container.close()

    assert disposed == [engine]
    assert [c for c in redis_closed if c is client] == [client]
    assert storage_closed == [storage]
    assert isinstance(storage, RedisStorage)
    assert redis_closed.count(storage.redis) == 1


async def test_request_scope_closes_its_session(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = spy(monkeypatch, AsyncSession, "close")
    container = build_test_container(settings, FakeTelegramSession(), i18n)
    try:
        async with container() as request:
            session = await request.get(AsyncSession)
            assert closed == []
        assert closed == [session]
    finally:
        await container.close()


async def test_app_resources_outlive_requests_while_request_ones_do_not(
    settings: Config, i18n: I18n
) -> None:
    container = build_test_container(settings, FakeTelegramSession(), i18n)
    try:
        async with container() as first:
            bot = await first.get(Bot)
            engine = await first.get(AsyncEngine)
            session = await first.get(AsyncSession)
            uow = await first.get(SQLAlchemyUnitOfWork)
        async with container() as second:
            assert await second.get(Bot) is bot
            assert await second.get(AsyncEngine) is engine
            assert await second.get(AsyncSession) is not session
            assert await second.get(SQLAlchemyUnitOfWork) is not uow
    finally:
        await container.close()


async def test_session_is_not_available_in_app_scope(
    settings: Config, i18n: I18n
) -> None:
    container = build_test_container(settings, FakeTelegramSession(), i18n)
    try:
        with pytest.raises(NoFactoryError):
            await container.get(AsyncSession)
    finally:
        await container.close()


async def test_event_isolation_shares_the_storage_client(settings: Config) -> None:
    container = build_container(settings)
    try:
        storage = await container.get(BaseStorage)
        isolation = await container.get(BaseEventIsolation)
        assert isinstance(storage, RedisStorage)
        assert isinstance(isolation, RedisEventIsolation)
        assert isolation.redis is storage.redis
        assert isolation.redis is not await container.get(Redis)
    finally:
        await container.close()


async def test_fsm_storage_owns_a_client_separate_from_the_shared_one(
    settings: Config,
) -> None:
    container = build_container(settings)
    try:
        storage = await container.get(BaseStorage)
        assert isinstance(storage, RedisStorage)
        assert storage.redis is not await container.get(Redis)
    finally:
        await container.close()


async def test_health_service_resolves_per_request_on_the_app_clients(
    settings: Config, i18n: I18n
) -> None:
    container = build_test_container(settings, FakeTelegramSession(), i18n)
    try:
        async with container() as first:
            service = await first.get(HealthService)
            assert service is await first.get(HealthService)
            assert service.session is await first.get(AsyncSession)
            assert service.redis is await container.get(Redis)
            assert service.repository is await container.get(SystemRepository)
        async with container() as second:
            assert await second.get(HealthService) is not service
        with pytest.raises(NoFactoryError):
            await container.get(HealthService)
    finally:
        await container.close()


@pytest.mark.parametrize(
    ("configured", "expected"), [("", "telegram-bot-test/0.0.0"), ("bot/2", "bot/2")]
)
async def test_the_user_agent_falls_back_to_the_project(
    settings: Config, i18n: I18n, configured: str, expected: str
) -> None:
    """A blank HTTP_USER_AGENT must still send an identifiable agent: some APIs
    refuse the library's default outright."""
    config = settings.model_copy(
        update={
            "http": settings.http.model_copy(update={"HTTP_USER_AGENT": configured})
        }
    )
    container = build_test_container(config, FakeTelegramSession(), i18n)
    try:
        assert await container.get(UserAgent) == expected
    finally:
        await container.close()


async def test_the_cache_is_one_redis_cache_per_process(settings: Config) -> None:
    # The production container: TestOverrides can replace the cache with an
    # in-process one. The Redis client connects lazily, so no Redis is needed
    # to build the cache.
    container = build_container(settings)
    try:
        first = await container.get(Cache)
        assert isinstance(first, RedisCache)
        assert await container.get(Cache) is first
    finally:
        await container.close()


class FakeSession(Provider):
    """A unit test must not reach PostgreSQL: TestOverrides keeps the real
    engine, which a commit would connect through."""

    @provide(scope=Scope.REQUEST, override=True)
    def session(self) -> AsyncSession:
        return FakeAsyncSession()


async def test_a_unit_of_work_publishes_after_commit(
    settings: Config, i18n: I18n
) -> None:
    """Without the publisher every task would wait for the sweeper, a minute
    or more, instead of starting at once."""
    session = FakeTelegramSession()
    overrides = TestOverrides(session, i18n)
    container = build_container(settings, overrides, FakeSession())
    try:
        async with container() as scope:
            uow = await scope.get(SQLAlchemyUnitOfWork)
            async with uow:
                await uow.enqueue_task("test.task", user_id=7)
                await uow.commit()
    finally:
        await container.close()
    assert overrides.publisher.published == [("test.task", [], {"user_id": 7})]


async def test_the_bots_broker_starts_and_stops_with_the_container(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot has no taskiq CLI to start and stop its broker; without the
    shutdown its Redis connection pool would outlive the container."""
    started = spy(monkeypatch, InMemoryBroker, "startup")
    stopped = spy(monkeypatch, InMemoryBroker, "shutdown")
    container = build_container(settings, SessionOnly(FakeTelegramSession(), i18n))
    try:
        broker = await container.get(AsyncBroker)
        assert isinstance(broker, InMemoryBroker)
        assert {"outbox.sweep", "outbox.purge"} <= set(broker.get_all_tasks())
        assert (started, stopped) == ([broker], [])
    finally:
        await container.close()
    assert stopped == [broker]
