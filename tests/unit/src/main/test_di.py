from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
import logging
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from aiogram.methods import SendMessage
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide
from dishka.exceptions import NoFactoryError
import pytest
from redis import exceptions as redis_exceptions
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from taskiq import AsyncBroker, InMemoryBroker
from taskiq.message import BrokerMessage

from src.broadcast.usecases.run_broadcast import RunBroadcastUseCase
from src.core.cache.interface import Cache, CacheKey
from src.core.cache.redis_cache import RedisCache
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.registry import event_subscribers, set_event_subscribers
from src.core.events.schemas import DomainEvent
from src.core.http.user_agent import UserAgent
from src.core.limiter.pacer import Pacer
from src.core.llm.tasks import LLMUsageRetention
from src.core.outbox.interface import TaskPublisher
from src.core.outbox.models import OutboxMessage
from src.core.outbox.publisher import UnregisteredTaskError
from src.core.redis.degradation import DegradationNotifier
from src.core.telegram.alerts import AlertBot, AlertSession, DeveloperAlerter
from src.core.telegram.middlewares.throttling import ThrottleLimiter
from src.core.telegram.session import ForbiddenMiddleware, RetryAfterMiddleware
from src.main import di as di_module
from src.main.config import BotConfig, Config
from src.main.di import build_container
from src.main.dispatcher import create_dispatcher
from src.system.repositories import SystemRepository
from src.system.services import HealthService
from src.user.schemas import TelegramProfile, UserProfile
from src.user.usecases.touch_user import TouchUserUseCase
from tests.factories.updates import build_message_update
from tests.fakes.db import FakeAsyncSession
from tests.fakes.redis import InMemoryRedis
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.logs import captured_logs
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


async def test_the_bots_broker_starts_on_its_first_send_and_stops_with_the_container(
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
        publisher = await container.get(TaskPublisher)
        assert started == []
        # Unregistered, so nothing runs inline; the start comes first.
        with pytest.raises(UnregisteredTaskError):
            await publisher.publish(uuid4(), "test.missing", [], {})
        assert (started, stopped) == ([broker], [])
    finally:
        await container.close()
    assert stopped == [broker]


class DownBroker(InMemoryBroker):
    """A broker whose tasks Redis is down: it cannot even start."""

    def __init__(self) -> None:
        super().__init__(await_inplace=True)
        self.starts = 0
        self.kicks = 0

    async def startup(self) -> None:
        self.starts += 1
        raise redis_exceptions.ConnectionError("tasks Redis down")

    async def kick(self, message: BrokerMessage) -> None:
        self.kicks += 1


class TasksRedisDown(Provider):
    """A fake database session, in-memory FSM state, and a TouchUserUseCase
    that enqueues a task the way a real use case does. The broker and the
    publisher stay the production ones: TestOverrides would replace the
    publisher."""

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[FakeAsyncSession] = []

    @provide(scope=Scope.APP, override=True)
    def fsm_storage(self) -> BaseStorage:
        return MemoryStorage()

    @provide(scope=Scope.APP, override=True)
    def event_isolation(self) -> BaseEventIsolation:
        return SimpleEventIsolation()

    @provide(scope=Scope.REQUEST, override=True)
    def session(self) -> AsyncSession:
        session = FakeAsyncSession()
        self.sessions.append(session)
        return session

    @provide(scope=Scope.REQUEST, override=True)
    def touch_user(self, uow: SQLAlchemyUnitOfWork) -> TouchUserUseCase:
        touch = AsyncMock(spec=TouchUserUseCase)
        touch.execute.side_effect = partial(_touch_and_enqueue, uow)
        return touch


async def _touch_and_enqueue(
    uow: SQLAlchemyUnitOfWork, profile: TelegramProfile
) -> UserProfile:
    async with uow:
        await uow.enqueue_task("test.task", user_id=profile.id)
        await uow.commit()
    return UserProfile(
        id=profile.id,
        first_name=profile.first_name,
        username=profile.username,
        last_name=profile.last_name,
        language_code=profile.language_code,
        locale=None,
        is_reachable=True,
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_a_tasks_redis_outage_does_not_fail_an_update(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every update resolves a unit of work, and with it the publisher: a
    broker started there would fail them all while the tasks Redis is down.
    The task enqueued meanwhile stays pending for the sweeper."""
    broker = DownBroker()
    monkeypatch.setattr(di_module, "build_broker", lambda _config: broker)
    session = FakeTelegramSession()
    down = TasksRedisDown()
    container = build_container(settings, SessionOnly(session, i18n), down)
    try:
        dp = await create_dispatcher(container)
        bot = await container.get(Bot)
        await dp.emit_startup(bot=bot)
        try:
            with captured_logs("src.core.outbox.publisher") as records:
                await dp.feed_update(bot, build_message_update("/start"))
        finally:
            await dp.emit_shutdown(bot=bot)
    finally:
        await container.close()
    assert len(session.sent(SendMessage)) == 1
    assert (broker.starts, broker.kicks) == (1, 0)
    [db_session] = down.sessions
    [(row,), _] = db_session.add.call_args
    assert isinstance(row, OutboxMessage)
    assert row.task_name == "test.task"
    # Logged as an outage, not a failed hook; never marked published.
    assert [r.levelno for r in records] == [logging.WARNING]


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, message: str) -> None:
        self.messages.append(message)


class DownRedis(Provider):
    """A Redis that refuses every command and a notifier that records,
    with the cache and the throttle limiter left real."""

    scope = Scope.APP

    def __init__(self) -> None:
        super().__init__()
        self.notifier = RecordingNotifier()

    @provide(override=True)
    def redis(self) -> Redis:
        redis = InMemoryRedis()
        redis.fail_next_commands(1000)
        return redis  # type: ignore[return-value]

    @provide(override=True)
    def notifier_override(self) -> DegradationNotifier:
        return self.notifier


async def test_a_redis_outage_reaches_the_developers(settings: Config) -> None:
    """The cache and the throttle limiter both run on without Redis; the
    outage would otherwise reach developers only through Sentry."""
    down = DownRedis()
    # The production container: TestOverrides replaces the cache and the
    # throttle limiter, and this is about the real ones.
    container = build_container(settings, down)
    try:
        cache = await container.get(Cache)
        await cache.get(CacheKey("probe", "1"))
        limiter = await container.get(ThrottleLimiter)
        await limiter.hit("probe", limit=1, window_ms=1000)
    finally:
        await container.close()
    assert len(down.notifier.messages) == 2
    assert any("Cache" in message for message in down.notifier.messages)
    assert any("Throttling" in message for message in down.notifier.messages)


async def test_the_alerter_is_the_degradation_notifier(
    settings: Config, i18n: I18n
) -> None:
    container = build_test_container(settings, FakeTelegramSession(), i18n)
    try:
        notifier = await container.get(DegradationNotifier)
        assert notifier is await container.get(DeveloperAlerter)
    finally:
        await container.close()


async def test_alerts_go_to_the_alert_chat_when_one_is_set(
    settings: Config, i18n: I18n
) -> None:
    config = settings.model_copy(
        update={
            "bot": settings.bot.model_copy(
                update={"DEV_ALERT_CHAT_ID": -100500, "DEVELOPER_IDS": frozenset({7})}
            )
        }
    )
    alerts = FakeTelegramSession()
    container = build_test_container(
        config, FakeTelegramSession(), i18n, alert_session=alerts
    )
    try:
        await (await container.get(DeveloperAlerter)).send("probe")
    finally:
        await container.close()
    assert [sent.chat_id for sent in alerts.sent(SendMessage)] == [-100500]


async def test_without_an_alert_chat_each_developer_is_alerted(
    settings: Config, i18n: I18n
) -> None:
    config = settings.model_copy(
        update={
            "bot": settings.bot.model_copy(
                update={"DEVELOPER_IDS": frozenset({8, 7}), "ADMIN_IDS": frozenset({9})}
            )
        }
    )
    alerts = FakeTelegramSession()
    container = build_test_container(
        config, FakeTelegramSession(), i18n, alert_session=alerts
    )
    try:
        await (await container.get(DeveloperAlerter)).send("probe")
    finally:
        await container.close()
    assert [sent.chat_id for sent in alerts.sent(SendMessage)] == [7, 8]


async def test_closing_the_container_waits_for_background_alerts(
    settings: Config, i18n: I18n
) -> None:
    """A degradation notice still in flight when the process stops would be
    cut off by the closing bot session."""
    config = settings.model_copy(
        update={
            "bot": settings.bot.model_copy(update={"DEVELOPER_IDS": frozenset({7})})
        }
    )
    alerts = FakeTelegramSession()
    container = build_test_container(
        config, FakeTelegramSession(), i18n, alert_session=alerts
    )
    try:
        (await container.get(DegradationNotifier)).notify("Redis is degraded")
    finally:
        await container.close()
    assert [sent.text for sent in alerts.sent(SendMessage)] == ["Redis is degraded"]


async def test_alerts_leave_through_a_bot_of_their_own(
    settings: Config, i18n: I18n
) -> None:
    """The handlers' Bot reports a Forbidden to the user domain, which needs
    the cache, which reports its outages through the alerter: sharing that Bot
    would be a dependency cycle."""
    alerts = FakeTelegramSession()
    container = build_test_container(
        settings, FakeTelegramSession(), i18n, alert_session=alerts
    )
    try:
        bot = await container.get(AlertBot)
        assert bot is not await container.get(Bot)
        assert bot.session is alerts
        assert bot.default.parse_mode == ParseMode.HTML
        # Flood control, and no ForbiddenMiddleware: the alert chat is no user.
        assert [type(m) for m in alerts.middleware] == [RetryAfterMiddleware]
    finally:
        await container.close()


async def test_closing_the_container_releases_the_alert_session(
    settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = spy(monkeypatch, AiohttpSession, "close")
    container = build_container(settings)
    bot_session = await container.get(BaseSession)
    alert_session = await container.get(AlertSession)
    assert alert_session is not bot_session

    await container.close()

    assert closed.count(alert_session) == 1
    assert closed.count(bot_session) == 1


async def test_every_broadcast_run_gets_its_own_pacer(
    settings: Config, i18n: I18n
) -> None:
    """A pacer shared across runs would carry one run's schedule into the next."""
    container = build_test_container(settings, FakeTelegramSession(), i18n)
    try:
        async with container() as first:
            await first.get(RunBroadcastUseCase)
            pacer = await first.get(Pacer)
        async with container() as second:
            assert await second.get(Pacer) is not pacer
    finally:
        await container.close()


async def test_llm_usage_retention_comes_from_the_config(
    settings: Config, i18n: I18n
) -> None:
    """The purge deletes everything older than this; a wrong unit would empty
    the table or keep it forever."""
    config = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"LLM_USAGE_RETENTION_DAYS": 45})}
    )
    container = build_container(config)
    try:
        assert await container.get(LLMUsageRetention) == timedelta(days=45)
    finally:
        await container.close()
