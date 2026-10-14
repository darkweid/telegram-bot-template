from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from aiogram.utils.i18n import I18n
from dishka.exceptions import NoFactoryError
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.telegram.session import ForbiddenMiddleware, RetryAfterMiddleware
from src.main.config import BotConfig, Config
from src.main.di import build_container
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.providers import build_test_container


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


async def test_request_scope_shares_one_session_and_unit_of_work(
    settings: Config, i18n: I18n
) -> None:
    container = build_test_container(settings, FakeTelegramSession(), i18n)
    try:
        async with container() as first:
            uow = await first.get(SQLAlchemyUnitOfWork)
            assert uow is await first.get(SQLAlchemyUnitOfWork)
            assert uow.session is await first.get(AsyncSession)
        async with container() as second:
            assert await second.get(SQLAlchemyUnitOfWork) is not uow
    finally:
        await container.close()


async def test_closing_the_container_releases_the_bot_session(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    closed: list[bool] = []
    original_close = session.close

    async def tracking_close() -> None:
        closed.append(True)
        await original_close()

    session.close = tracking_close  # type: ignore[method-assign]
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
