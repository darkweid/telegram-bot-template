"""The composition root: everything with a lifetime is created here, once,
and released when the container closes."""

from collections.abc import AsyncIterator

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.types import LinkPreviewOptions
from aiogram.utils.i18n import I18n
from dishka import (
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

from src.core.database.engine import create_engine
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.i18n import LOCALES_DIR, build_i18n
from src.core.redis.core import create_redis_client
from src.core.telegram.fsm import create_event_isolation, create_fsm_storage
from src.core.telegram.session import (
    BlockedUserRecorder,
    ForbiddenMiddleware,
    RetryAfterMiddleware,
)
from src.main.config import (
    AppConfig,
    BotConfig,
    Config,
    PostgresConfig,
    RedisConfig,
    WebhookConfig,
    WebServerConfig,
)
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
    def web_server(self, config: Config) -> WebServerConfig:
        return config.web_server

    @provide
    def webhook(self, config: Config) -> WebhookConfig:
        return config.webhook

    @provide
    def postgres(self, config: Config) -> PostgresConfig:
        return config.postgres

    @provide
    def redis(self, config: Config) -> RedisConfig:
        return config.redis


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

    uow = provide(SQLAlchemyUnitOfWork, scope=Scope.REQUEST)


class RedisProvider(Provider):
    scope = Scope.APP

    @provide
    async def client(self, redis: RedisConfig) -> AsyncIterator[Redis]:
        client = create_redis_client(redis.dsn)
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
        recorder: BlockedUserRecorder,
    ) -> Bot:
        # Retry outermost, so a retried call that ends in Forbidden is
        # recorded once.
        session.middleware(RetryAfterMiddleware(bot_config.TELEGRAM_RETRY_ATTEMPTS))
        session.middleware(ForbiddenMiddleware(recorder))
        return Bot(
            token=bot_config.BOT_TOKEN.get_secret_value(),
            session=session,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                # A link in a reply would otherwise unfold into a preview card
                # nobody asked for.
                link_preview=LinkPreviewOptions(is_disabled=True),
            ),
        )


class I18nProvider(Provider):
    scope = Scope.APP

    @provide
    def i18n(self, bot_config: BotConfig) -> I18n:
        return build_i18n(LOCALES_DIR, default_locale=bot_config.DEFAULT_LOCALE)


def build_container(config: Config, *providers: Provider) -> AsyncContainer:
    """The bot's container. `providers` override production ones in tests."""
    return make_async_container(
        ConfigProvider(),
        DatabaseProvider(),
        RedisProvider(),
        TelegramProvider(),
        I18nProvider(),
        UserProvider(),
        SystemProvider(),
        AiogramProvider(),
        *providers,
        context={Config: config},
        # An override that replaces nothing, or a factory registered twice by
        # accident, is a startup error rather than a silent surprise.
        validation_settings=STRICT_VALIDATION,
    )
