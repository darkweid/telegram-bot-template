"""The composition root: everything with a lifetime is created here, once,
and released when the container closes."""

from collections.abc import AsyncIterator, Mapping

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.types import LinkPreviewOptions
from aiogram.utils.i18n import I18n
from dishka import (
    AnyOf,
    AsyncContainer,
    Provider,
    Scope,
    from_context,
    make_async_container,
    provide,
)
from dishka.entities.validation_settings import STRICT_VALIDATION
from dishka.integrations.aiogram import AiogramProvider
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from taskiq import AsyncBroker

from src.broadcast.di import BroadcastProvider
from src.core.cache.interface import Cache
from src.core.cache.redis_cache import RedisCache
from src.core.cache.serializer import JsonSerializer
from src.core.database.engine import create_engine
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.registry import set_event_subscribers
from src.core.http.user_agent import UserAgent, build_user_agent
from src.core.i18n import LOCALES_DIR, build_i18n
from src.core.limiter.limiter import RateLimiter
from src.core.outbox.interface import TaskPublisher
from src.core.outbox.publisher import OutboxPublisher
from src.core.redis.core import create_redis_client
from src.core.redis.degradation import DegradationNotifier, RedisDegradationReporter
from src.core.tasks.broker import TasksRedis
from src.core.telegram.alerts import (
    AlertBot,
    AlertLimiter,
    AlertSession,
    DeveloperAlerter,
)
from src.core.telegram.fsm import create_event_isolation, create_fsm_storage
from src.core.telegram.middlewares.throttling import (
    Throttle,
    ThrottleLimiter,
    ThrottleRule,
    ThrottlingMiddleware,
)
from src.core.telegram.roles import Roles
from src.core.telegram.session import (
    ForbiddenMiddleware,
    RetryAfterMiddleware,
    UnreachableUserRecorder,
)
from src.main.config import (
    AppConfig,
    BotConfig,
    BroadcastConfig,
    CacheConfig,
    Config,
    HttpConfig,
    PostgresConfig,
    RedisConfig,
    ThrottlingConfig,
    WebhookConfig,
    WebServerConfig,
)
from src.main.event_subscribers import EVENT_SUBSCRIBERS
from src.main.tasks import build_broker
from src.note.di import NoteProvider
from src.system.di import SystemProvider
from src.user.di import UserProvider


class ConfigProvider(Provider):
    scope = Scope.APP

    config = from_context(provides=Config)

    @provide
    def app(self, config: Config) -> AppConfig:
        return config.app

    @provide
    def bot(self, config: Config) -> BotConfig:
        return config.bot

    @provide
    def throttling(self, config: Config) -> ThrottlingConfig:
        return config.throttling

    @provide
    def broadcast(self, config: Config) -> BroadcastConfig:
        return config.broadcast

    @provide
    def roles(self, bot: BotConfig) -> Roles:
        return Roles(admins=bot.ADMIN_IDS, developers=bot.DEVELOPER_IDS)

    @provide
    def web_server(self, config: Config) -> WebServerConfig:
        return config.web_server

    @provide
    def webhook(self, config: Config) -> WebhookConfig:
        return config.webhook

    @provide
    def http(self, config: Config) -> HttpConfig:
        return config.http

    @provide
    def postgres(self, config: Config) -> PostgresConfig:
        return config.postgres

    @provide
    def redis(self, config: Config) -> RedisConfig:
        return config.redis

    @provide
    def cache(self, config: Config) -> CacheConfig:
        return config.cache


class DatabaseProvider(Provider):
    @provide(scope=Scope.APP)
    async def engine(self, postgres: PostgresConfig) -> AsyncIterator[AsyncEngine]:
        idle_timeout = postgres.DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS
        engine = create_engine(
            postgres.dsn_async,
            echo=postgres.DB_ECHO,
            pool_size=postgres.DB_POOL_SIZE,
            max_overflow=postgres.DB_MAX_OVERFLOW,
            statement_timeout_seconds=postgres.DB_STATEMENT_TIMEOUT_SECONDS,
            idle_in_transaction_timeout_seconds=idle_timeout,
        )
        yield engine
        await engine.dispose()

    @provide(scope=Scope.APP)
    def sessionmaker(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(bind=engine, expire_on_commit=False)

    @provide(scope=Scope.REQUEST)
    async def session(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    # Not cached: a unit of work is single-use, and one update can run several
    # use cases (UserMiddleware's, then the handler's) over its one session.
    @provide(scope=Scope.REQUEST, cache=False)
    def uow(
        self, session: AsyncSession, publisher: TaskPublisher
    ) -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session, publisher=publisher)


class RedisProvider(Provider):
    scope = Scope.APP

    @provide
    async def client(self, redis: RedisConfig) -> AsyncIterator[Redis]:
        client = create_redis_client(redis.dsn)
        yield client
        await client.aclose()

    @provide
    async def tasks_client(self, redis: RedisConfig) -> AsyncIterator[TasksRedis]:
        # The task queue's database: heartbeat and queue length are read here.
        client = TasksRedis(create_redis_client(redis.tasks_dsn))
        yield client
        await client.aclose()

    @provide
    async def fsm_storage(self, redis: RedisConfig) -> AsyncIterator[BaseStorage]:
        storage = create_fsm_storage(redis.fsm_dsn)
        # The storage keeps its own client on purpose: its close() shuts the
        # connection pool, and the dispatcher calls it on shutdown, which would
        # pull the pool from under a client shared with the rest of the app.
        yield storage
        # The dispatcher closes its storage on shutdown as well; closing a
        # closed Redis client is a no-op, and this covers a run that fails
        # before the dispatcher ever starts.
        await storage.close()

    @provide
    def event_isolation(self, storage: BaseStorage) -> BaseEventIsolation:
        # Reuses the storage's Redis client, so there is nothing to tear down here.
        return create_event_isolation(storage)


class CacheProvider(Provider):
    scope = Scope.APP

    @provide
    def cache(
        self, redis: Redis, config: CacheConfig, notifier: DegradationNotifier
    ) -> Cache:
        return RedisCache(
            redis,
            JsonSerializer(),
            prefix=config.CACHE_KEY_PREFIX,
            default_ttl=config.CACHE_DEFAULT_TTL,
            version_ttl=config.CACHE_VERSION_TTL,
            enabled=config.CACHE_ENABLED,
            reporter=RedisDegradationReporter("Cache", notifier=notifier),
        )


class ThrottlingProvider(Provider):
    scope = Scope.APP

    @provide
    def limiter(self, redis: Redis, notifier: DegradationNotifier) -> ThrottleLimiter:
        return ThrottleLimiter(
            RateLimiter(
                redis,
                prefix="throttle",
                reporter=RedisDegradationReporter("Throttling", notifier=notifier),
            )
        )

    @provide
    def middleware(
        self, limiter: ThrottleLimiter, throttling: ThrottlingConfig
    ) -> ThrottlingMiddleware:
        return ThrottlingMiddleware(
            limiter,
            {
                Throttle.DEFAULT: ThrottleRule(
                    throttling.THROTTLE_DEFAULT_LIMIT,
                    throttling.THROTTLE_DEFAULT_WINDOW_SECONDS,
                ),
                Throttle.LLM: ThrottleRule(
                    throttling.THROTTLE_LLM_LIMIT,
                    throttling.THROTTLE_LLM_WINDOW_SECONDS,
                ),
            },
        )


def _bot_defaults() -> DefaultBotProperties:
    return DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        # A link in a reply would otherwise unfold into a preview card nobody
        # asked for.
        link_preview=LinkPreviewOptions(is_disabled=True),
    )


class TelegramProvider(Provider):
    scope = Scope.APP

    @provide
    async def session(self) -> AsyncIterator[BaseSession]:
        session = AiohttpSession()
        yield session
        await session.close()

    @provide
    def bot(
        self,
        bot_config: BotConfig,
        session: BaseSession,
        recorder: UnreachableUserRecorder,
    ) -> Bot:
        # Retry outermost, so a retried call that ends in Forbidden is
        # recorded once.
        session.middleware(RetryAfterMiddleware(bot_config.TELEGRAM_RETRY_ATTEMPTS))
        session.middleware(ForbiddenMiddleware(recorder))
        return Bot(
            token=bot_config.BOT_TOKEN.get_secret_value(),
            session=session,
            default=_bot_defaults(),
        )


class AlertProvider(Provider):
    scope = Scope.APP

    @provide
    async def session(self) -> AsyncIterator[AlertSession]:
        session = AiohttpSession()
        yield AlertSession(session)
        await session.close()

    @provide
    def bot(self, bot_config: BotConfig, session: AlertSession) -> AlertBot:
        # Flood control only: no ForbiddenMiddleware, see AlertBot.
        session.middleware(RetryAfterMiddleware(bot_config.TELEGRAM_RETRY_ATTEMPTS))
        return AlertBot(
            Bot(
                token=bot_config.BOT_TOKEN.get_secret_value(),
                session=session,
                default=_bot_defaults(),
            )
        )

    @provide
    def limiter(self, redis: Redis) -> AlertLimiter:
        # No notifier: an outage of this limiter is reported by its siblings,
        # and an alert about the alerter would go through itself.
        return AlertLimiter(
            RateLimiter(
                redis, prefix="alerts", reporter=RedisDegradationReporter("Alerts")
            )
        )

    @provide(provides=AnyOf[DeveloperAlerter, DegradationNotifier])
    async def alerter(
        self,
        bot: AlertBot,
        limiter: AlertLimiter,
        bot_config: BotConfig,
        roles: Roles,
    ) -> AsyncIterator[DeveloperAlerter]:
        recipients = (
            (bot_config.DEV_ALERT_CHAT_ID,)
            if bot_config.DEV_ALERT_CHAT_ID is not None
            else tuple(sorted(roles.developers))
        )
        alerter = DeveloperAlerter(
            bot,
            limiter,
            recipients=recipients,
            cooldown_seconds=bot_config.ALERT_COOLDOWN_SECONDS,
        )
        yield alerter
        # Created after the alert Bot, so finalized before its session closes.
        await alerter.drain()


class I18nProvider(Provider):
    scope = Scope.APP

    @provide
    def i18n(self, bot_config: BotConfig) -> I18n:
        return build_i18n(LOCALES_DIR, default_locale=bot_config.DEFAULT_LOCALE)


class HttpProvider(Provider):
    scope = Scope.APP

    @provide
    def user_agent(self, http: HttpConfig, app: AppConfig) -> UserAgent:
        return UserAgent(
            http.HTTP_USER_AGENT or build_user_agent(app.PROJECT_NAME, app.VERSION)
        )


class BrokerProvider(Provider):
    """The bot's side of the task queue: it only sends.

    The worker gets its broker from the taskiq CLI instead (WorkerProvider),
    which also starts and stops it. The bot's broker is started by the
    publisher on its first send, not here: a unit of work needs the publisher,
    and a start in this factory would fail every update, the ones that enqueue
    nothing included, while the tasks Redis is down. A wrong
    REDIS_TASKS_DATABASE is still caught before the first update: the startup
    check pings the same database through TasksRedis.
    """

    scope = Scope.APP

    @provide
    async def broker(self, config: Config) -> AsyncIterator[AsyncBroker]:
        broker = build_broker(config)
        yield broker
        # Safe on a broker that never started: it closes the connection pool
        # and runs the shutdown hooks, nothing more.
        await broker.shutdown()

    @provide
    def publisher(
        self, broker: AsyncBroker, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> TaskPublisher:
        return OutboxPublisher(broker, sessionmaker, start_broker=True)


def build_container(
    config: Config,
    *providers: Provider,
    context: Mapping[object, object] | None = None,
) -> AsyncContainer:
    """The bot's or the worker's container. `providers` override production
    ones in tests; `context` adds values the process already holds (the
    worker's broker)."""
    # Every process that builds a container (bot, worker, tests) runs the
    # same subscribers; the registry is replaced, never merged.
    set_event_subscribers(EVENT_SUBSCRIBERS)
    return make_async_container(
        ConfigProvider(),
        DatabaseProvider(),
        RedisProvider(),
        CacheProvider(),
        ThrottlingProvider(),
        TelegramProvider(),
        AlertProvider(),
        I18nProvider(),
        HttpProvider(),
        BrokerProvider(),
        UserProvider(),
        BroadcastProvider(),
        NoteProvider(),
        SystemProvider(),
        AiogramProvider(),
        *providers,
        context={Config: config, **(context or {})},
        # An override that replaces nothing, or a factory registered twice by
        # accident, is a startup error rather than a silent surprise.
        validation_settings=STRICT_VALIDATION,
    )
