"""The composition root: everything with a lifetime is created here, once,
and released when the container closes."""

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from datetime import timedelta
from types import MappingProxyType
from typing import assert_never

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

from src.admin.di import AdminProvider
from src.broadcast.di import BroadcastProvider
from src.core.cache.interface import Cache
from src.core.cache.redis_cache import RedisCache
from src.core.cache.serializer import JsonSerializer
from src.core.database.engine import create_engine
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.email.disabled import DisabledMailer
from src.core.email.interface import Mailer
from src.core.email.service import EmailService
from src.core.email.smtp_mailer import SmtpMailer, SmtpSettings
from src.core.events.registry import set_event_subscribers
from src.core.http.user_agent import UserAgent, build_user_agent
from src.core.i18n import LOCALES_DIR, build_i18n
from src.core.limiter.limiter import RateLimiter
from src.core.llm.disabled import DisabledLLMClient
from src.core.llm.enums import LLMProviderName, ModelTier
from src.core.llm.fallback import FallbackLLMClient
from src.core.llm.interface import LLMClient
from src.core.llm.providers.anthropic import AnthropicLLMClient
from src.core.llm.providers.openrouter import OpenRouterLLMClient
from src.core.llm.usage.pricing import PricingTable
from src.core.llm.usage.recording import LLMUsageRecorder, RecordingLLMClient
from src.core.llm.usage.repository import LLMUsageRepository
from src.core.llm.usage.tasks import LLMUsageRetention
from src.core.outbox.interface import TaskPublisher
from src.core.outbox.publisher import OutboxPublisher
from src.core.redis.core import create_redis_client
from src.core.redis.degradation import DegradationNotifier, RedisDegradationReporter
from src.core.storage.s3.adapter import S3Adapter
from src.core.storage.s3.disabled import DisabledS3Client
from src.core.storage.s3.interface import S3Client
from src.core.tasks.broker import TasksRedis
from src.core.telegram.alerts import (
    AlertBot,
    AlertLimiter,
    AlertSession,
    DeveloperAlerter,
)
from src.core.telegram.background import BackgroundRunner
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
    EmailConfig,
    HttpConfig,
    LLMConfig,
    PostgresConfig,
    RedisConfig,
    S3Config,
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

    @provide
    def s3(self, config: Config) -> S3Config:
        return config.s3

    @provide
    def email(self, config: Config) -> EmailConfig:
        return config.email


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

    @provide
    async def background_runner(
        self, container: AsyncContainer, alerter: DeveloperAlerter
    ) -> AsyncIterator[BackgroundRunner]:
        # An APP factory asking for AsyncContainer gets the root container,
        # which outlives every update.
        runner = BackgroundRunner(container, alerter)
        yield runner
        # A bot that stopped normally has drained already; a container closed
        # without that (tests, a failed start) leaves no task behind.
        await runner.drain(0)


def _required(value: str, key: str) -> str:
    # Config's validator refuses a provider without its key or model; a Config
    # copied around the validator still fails here by the key's name instead
    # of at the first paid call.
    if not value:
        raise RuntimeError(f"{key} is required by the configured LLM provider")
    return value


def _tier_models(strong: str, fast: str) -> Mapping[ModelTier, str]:
    # An empty fast model means the strong one: a bot that sets one model
    # calls it for every tier.
    return MappingProxyType({ModelTier.STRONG: strong, ModelTier.FAST: fast or strong})


def _build_adapter(
    name: LLMProviderName,
    models: Mapping[ModelTier, str],
    llm: LLMConfig,
    pricing: PricingTable,
) -> AnthropicLLMClient | OpenRouterLLMClient:
    match name:
        case LLMProviderName.ANTHROPIC:
            return AnthropicLLMClient(
                api_key=_required(
                    llm.ANTHROPIC_API_KEY.get_secret_value(), "ANTHROPIC_API_KEY"
                ),
                models=models,
                pricing=pricing,
                timeout_seconds=llm.LLM_TIMEOUT_SECONDS,
                max_retries=llm.LLM_MAX_RETRIES,
            )
        case LLMProviderName.OPENROUTER:
            return OpenRouterLLMClient(
                api_key=_required(
                    llm.OPENROUTER_API_KEY.get_secret_value(), "OPENROUTER_API_KEY"
                ),
                models=models,
                app_url=llm.OPENROUTER_APP_URL,
                app_title=llm.OPENROUTER_APP_TITLE,
                timeout_seconds=llm.LLM_TIMEOUT_SECONDS,
                max_retries=llm.LLM_MAX_RETRIES,
            )
        case _:
            assert_never(name)


def _recorded_adapter(
    name: LLMProviderName,
    models: Mapping[ModelTier, str],
    llm: LLMConfig,
    pricing: PricingTable,
    recorder: LLMUsageRecorder,
    adapters: AsyncExitStack,
) -> RecordingLLMClient:
    adapter = _build_adapter(name, models, llm, pricing)
    adapters.push_async_callback(adapter.aclose)
    return RecordingLLMClient(
        adapter,
        recorder,
        provider=name,
        models=models,
        log_content=llm.LLM_LOG_CONTENT,
    )


class LLMProvider(Provider):
    """The model client, one per process. Its SDK clients close with the
    container: the bot's at shutdown, the worker's in WorkerLifecycle."""

    scope = Scope.APP

    usage = provide(LLMUsageRepository)
    recorder = provide(LLMUsageRecorder)

    @provide
    def pricing(self, config: Config) -> PricingTable:
        return PricingTable(config.llm.LLM_PRICING_OVERRIDES)

    @provide
    def usage_retention(self, config: Config) -> LLMUsageRetention:
        return LLMUsageRetention(timedelta(days=config.llm.LLM_USAGE_RETENTION_DAYS))

    @provide
    async def client(
        self,
        config: Config,
        pricing: PricingTable,
        recorder: LLMUsageRecorder,
        alerter: DeveloperAlerter,
    ) -> AsyncIterator[LLMClient]:
        llm = config.llm
        if llm.LLM_PROVIDER is None:
            yield DisabledLLMClient()
            return
        # Every adapter built is closed on the way out, the primary included
        # when the fallback cannot be built.
        async with AsyncExitStack() as adapters:
            primary = _recorded_adapter(
                llm.LLM_PROVIDER,
                _tier_models(llm.LLM_MODEL, llm.LLM_MODEL_FAST),
                llm,
                pricing,
                recorder,
                adapters,
            )
            if llm.LLM_FALLBACK_PROVIDER is None:
                yield primary
                return
            fallback = _recorded_adapter(
                llm.LLM_FALLBACK_PROVIDER,
                _tier_models(
                    _required(llm.LLM_FALLBACK_MODEL, "LLM_FALLBACK_MODEL"),
                    llm.LLM_FALLBACK_MODEL_FAST,
                ),
                llm,
                pricing,
                recorder,
                adapters,
            )
            yield FallbackLLMClient(primary, fallback, alerter)


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


def build_s3_adapter(s3: S3Config) -> S3Adapter:
    """The adapter, not yet entered; S3Config has checked the credentials."""
    return S3Adapter(
        bucket=s3.S3_BUCKET_NAME,
        region=s3.S3_REGION_NAME,
        access_key=s3.S3_ACCESS_KEY_ID,
        secret_key=s3.S3_SECRET_ACCESS_KEY.get_secret_value(),
        default_presign_ttl=s3.S3_PRE_SIGNED_URL_SECONDS,
        endpoint_url=s3.S3_ENDPOINT_URL,
        addressing_style=s3.S3_ADDRESSING_STYLE,
        signature_version=s3.S3_SIGNATURE_VERSION,
        verify_ssl=s3.S3_VERIFY_SSL,
        ca_bundle=s3.S3_CA_BUNDLE,
        treat_access_denied_as_missing=s3.S3_TREAT_ACCESS_DENIED_AS_MISSING,
        connect_timeout_seconds=s3.S3_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds=s3.S3_READ_TIMEOUT_SECONDS,
        retry_max_attempts=s3.S3_RETRY_MAX_ATTEMPTS,
        retry_mode=s3.S3_RETRY_MODE,
        max_upload_size_bytes=s3.S3_MAX_UPLOAD_SIZE_BYTES,
    )


class S3Provider(Provider):
    scope = Scope.APP

    @provide
    async def client(self, s3: S3Config) -> AsyncIterator[S3Client]:
        if not s3.S3_ENABLED:
            yield DisabledS3Client()
            return
        # Opened on the first request for it and closed with the container:
        # at the end of bot_runtime in the bot, on worker shutdown in the worker.
        async with build_s3_adapter(s3) as adapter:
            yield adapter


class EmailProvider(Provider):
    scope = Scope.APP

    @provide
    def mailer(self, email: EmailConfig) -> Mailer:
        if not email.EMAIL_ENABLED:
            return DisabledMailer()
        return SmtpMailer(
            SmtpSettings(
                host=email.EMAIL_SERVER,
                port=email.EMAIL_PORT,
                username=email.EMAIL_USER,
                password=email.EMAIL_PASSWORD.get_secret_value(),
                from_name=email.EMAIL_FROM_NAME,
                use_tls=email.EMAIL_USE_TLS,
                start_tls=email.EMAIL_STARTTLS,
                validate_certs=email.EMAIL_VALIDATE_CERTS,
            )
        )

    @provide
    def service(self, mailer: Mailer, email: EmailConfig, s3: S3Config) -> EmailService:
        return EmailService(
            mailer, email_enabled=email.EMAIL_ENABLED, s3_enabled=s3.S3_ENABLED
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
        LLMProvider(),
        S3Provider(),
        EmailProvider(),
        BrokerProvider(),
        UserProvider(),
        AdminProvider(),
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
