from functools import lru_cache
import os
import re
from typing import Annotated, Literal, Self
from urllib.parse import quote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.core.i18n import SUPPORTED_LOCALES


class RedisConfig(BaseSettings):
    REDIS_HOST: str
    REDIS_PORT: int = Field(ge=1, le=65535)
    REDIS_PASSWORD: str
    REDIS_DATABASE: str
    REDIS_FSM_DATABASE: str = "2"
    REDIS_TASKS_DATABASE: str = "1"

    model_config = SettingsConfigDict(extra="ignore")

    def _dsn(self, database: str) -> str:
        # quote(safe="") because generated passwords routinely contain @ / % :
        # and every URL parser downstream would silently misread the DSN.
        return (
            f"redis://:"
            f"{quote(self.REDIS_PASSWORD, safe='')}@"
            f"{self.REDIS_HOST}:"
            f"{self.REDIS_PORT}/"
            f"{database}"
        )

    @property
    def dsn(self) -> str:
        return self._dsn(self.REDIS_DATABASE)

    @property
    def fsm_dsn(self) -> str:
        return self._dsn(self.REDIS_FSM_DATABASE)

    @property
    def tasks_dsn(self) -> str:
        return self._dsn(self.REDIS_TASKS_DATABASE)


class CacheConfig(BaseSettings):
    # Off: every read misses and nothing is written; the bot keeps working,
    # every update just reads the database.
    CACHE_ENABLED: bool = True
    CACHE_DEFAULT_TTL: int = Field(60, gt=0)
    # How long a namespace's version counter lives. A cached value never
    # outlives it: a TTL above it is rejected when the value is written.
    CACHE_VERSION_TTL: int = Field(604_800, gt=0)
    CACHE_KEY_PREFIX: str = "cache"

    model_config = SettingsConfigDict(extra="ignore")

    @model_validator(mode="after")
    def default_ttl_fits_the_version_ttl(self) -> Self:
        # Checked at startup: otherwise every write without an explicit TTL
        # raises when the update runs, and the cache fails every update that
        # touches it.
        if self.CACHE_DEFAULT_TTL > self.CACHE_VERSION_TTL:
            raise ValueError("CACHE_DEFAULT_TTL must not exceed CACHE_VERSION_TTL")
        return self


class SentryConfig(BaseSettings):
    SENTRY_DSN: str | None = None
    SENTRY_ENV: str = "development"
    SENTRY_ENABLED: bool = False

    model_config = SettingsConfigDict(extra="ignore")


class PostgresConfig(BaseSettings):
    DB_ECHO: bool

    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_HOST: str
    POSTGRES_PORT: int = Field(ge=1, le=65535)
    POSTGRES_DB: str

    DB_POOL_SIZE: int = Field(5, gt=0)
    DB_MAX_OVERFLOW: int = Field(2, ge=0)
    # Server-side session limits, sent as connection startup parameters; 0 turns
    # one off. A handler whose code awaits something slow inside an open
    # transaction, or a query gone wrong, otherwise holds its locks and its
    # connection for as long as it likes. A transaction that needs more time
    # raises its own cap with set_local_statement_timeout.
    DB_STATEMENT_TIMEOUT_SECONDS: float = Field(30, ge=0)
    DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS: float = Field(60, ge=0)

    model_config = SettingsConfigDict(extra="ignore")

    @field_validator("POSTGRES_DB")
    @classmethod
    def reject_url_breaking_database_name(cls, value: str) -> str:
        # SQLAlchemy's make_url treats everything after '?' as the query string
        # and does not decode the database path segment, so a name with '?' can
        # be neither passed raw (truncated) nor percent-encoded (renamed).
        # Every other character survives make_url unchanged and is allowed.
        if "?" in value:
            raise ValueError(
                "POSTGRES_DB must not contain '?': such a name cannot be "
                "represented in a database URL"
            )
        return value

    @property
    def dsn_async(self) -> str:
        # quote(safe="") because generated credentials routinely contain @ / % :
        # and every URL parser downstream would silently misread the DSN.
        return (
            f"postgresql+asyncpg://"
            f"{quote(self.POSTGRES_USER, safe='')}:"
            f"{quote(self.POSTGRES_PASSWORD, safe='')}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/"
            f"{self.POSTGRES_DB}"
        )

    @property
    def dsn_sync(self) -> str:
        return (
            f"postgresql://"
            f"{quote(self.POSTGRES_USER, safe='')}:"
            f"{quote(self.POSTGRES_PASSWORD, safe='')}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/"
            f"{self.POSTGRES_DB}"
        )


class AppConfig(BaseSettings):
    PROJECT_NAME: str
    VERSION: str
    DEBUG: bool = False
    TESTING: bool = False
    # Read by loggers/ straight from the environment at import time as well, so
    # that a module logging during import already uses the configured level.
    LOG_LEVEL: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    LOG_JSON: bool = False

    model_config = SettingsConfigDict(extra="ignore")


# Telegram issues tokens as "<bot id>:<35 characters>"; the length check is a
# floor rather than an exact match so a future longer secret part still passes.
_BOT_TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")


class BotConfig(BaseSettings):
    BOT_TOKEN: SecretStr
    BOT_MODE: Literal["polling", "webhook"] = "polling"
    DEFAULT_LOCALE: str = "en"
    # A user row is rewritten when the Telegram profile changes, or when
    # last_seen_at is older than this; every other update reads it and moves on.
    USER_TOUCH_INTERVAL_SECONDS: int = Field(300, gt=0)
    # How long a user's snapshot answers updates from the cache. A language
    # change, a ban and a reachability change drop it at once; anything else
    # that edits the row by hand shows up after at most this long.
    USER_CACHE_TTL_SECONDS: int = Field(300, gt=0)
    # Flood-wait retries per Bot API call (src/core/telegram/session.py).
    TELEGRAM_RETRY_ATTEMPTS: int = Field(3, ge=0, le=10)
    # How long a stopping bot waits for updates already being handled. Keep it
    # below the container's stop_grace_period, or Docker kills the process first.
    SHUTDOWN_TIMEOUT_SECONDS: float = Field(20, gt=0)
    # Telegram user ids, comma-separated. Admins get the admin commands and
    # menu, developers the developer ones; both skip throttling and bans.
    # NoDecode: without it pydantic-settings expects a JSON list here.
    ADMIN_IDS: Annotated[frozenset[int], NoDecode] = frozenset()
    DEVELOPER_IDS: Annotated[frozenset[int], NoDecode] = frozenset()

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    @field_validator("ADMIN_IDS", "DEVELOPER_IDS", mode="before")
    @classmethod
    def split_user_ids(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return frozenset(int(part) for part in value.split(",") if part.strip())

    @field_validator("ADMIN_IDS", "DEVELOPER_IDS")
    @classmethod
    def require_user_ids(cls, value: frozenset[int]) -> frozenset[int]:
        # User ids are positive; a negative one is a group or a channel.
        if any(user_id <= 0 for user_id in value):
            raise ValueError("role ids must be Telegram user ids (positive integers)")
        return value

    @field_validator("BOT_TOKEN")
    @classmethod
    def validate_bot_token(cls, value: SecretStr) -> SecretStr:
        if not _BOT_TOKEN_PATTERN.fullmatch(value.get_secret_value()):
            raise ValueError("BOT_TOKEN does not look like a token from @BotFather")
        return value

    @field_validator("DEFAULT_LOCALE")
    @classmethod
    def validate_default_locale(cls, value: str) -> str:
        locale = value.strip().lower()
        if locale not in SUPPORTED_LOCALES:
            raise ValueError(
                f"DEFAULT_LOCALE must be one of {', '.join(SUPPORTED_LOCALES)}"
            )
        return locale


class ThrottlingConfig(BaseSettings):
    # Handler calls per user per window (src/core/telegram/middlewares/throttling.py).
    # Admins and developers are never limited.
    THROTTLE_DEFAULT_LIMIT: int = Field(20, ge=1)
    THROTTLE_DEFAULT_WINDOW_SECONDS: int = Field(60, ge=1)
    # Handlers flagged throttle="llm" call a paid model.
    THROTTLE_LLM_LIMIT: int = Field(5, ge=1)
    THROTTLE_LLM_WINDOW_SECONDS: int = Field(60, ge=1)

    model_config = SettingsConfigDict(extra="ignore")


class WebServerConfig(BaseSettings):
    # The aiohttp server runs in both modes: health endpoints always, the
    # Telegram webhook only when BOT_MODE=webhook. Inside the container it
    # listens on every interface; compose keeps the port on the internal network.
    WEB_SERVER_HOST: str = "0.0.0.0"  # nosec B104
    WEB_SERVER_PORT: int = Field(8080, ge=1, le=65535)

    model_config = SettingsConfigDict(extra="ignore")


_WEBHOOK_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
# Telegram's own rule for secret_token is 1-256 characters of this set; 32 is
# ours, so the secret is never short enough to guess.
_WEBHOOK_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")


class WebhookConfig(BaseSettings):
    # Required only when BOT_MODE=webhook (checked on Config, which sees both).
    WEBHOOK_BASE_URL: str = ""
    WEBHOOK_PATH: str = "/webhook/telegram"
    WEBHOOK_SECRET: SecretStr = SecretStr("")
    WEBHOOK_MAX_CONNECTIONS: int = Field(40, ge=1, le=100)

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    @field_validator("WEBHOOK_PATH")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if len(value) > 256:
            raise ValueError("WEBHOOK_PATH must be at most 256 characters")
        # Every segment is non-empty and holds at least one letter, digit or
        # underscore, so "//", "/a//b", "/-" and a trailing slash are rejected.
        segments = value[1:].split("/")
        if not value.startswith("/") or not all(
            _WEBHOOK_SEGMENT_PATTERN.fullmatch(segment) and segment.strip("-")
            for segment in segments
        ):
            raise ValueError(
                "WEBHOOK_PATH must start with '/' and consist of non-empty segments "
                "of letters, digits, '_' and '-'"
            )
        return value

    @field_validator("WEBHOOK_SECRET")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if secret and not _WEBHOOK_SECRET_PATTERN.fullmatch(secret):
            raise ValueError(
                "WEBHOOK_SECRET must be 32-256 characters of A-Z, a-z, 0-9, '_' and '-'"
            )
        return value

    @property
    def url(self) -> str:
        return self.WEBHOOK_BASE_URL.rstrip("/") + self.WEBHOOK_PATH


class HttpConfig(BaseSettings):
    # Sent on every outgoing request (src/core/http). Blank derives
    # "<project-name>/<VERSION>" from PROJECT_NAME and VERSION. Many APIs
    # behind a WAF refuse the library's default agent outright.
    HTTP_USER_AGENT: str = Field("", max_length=256)

    model_config = SettingsConfigDict(extra="ignore")

    @field_validator("HTTP_USER_AGENT")
    @classmethod
    def reject_unprintable(cls, value: str) -> str:
        # A control character in a header is a request-splitting vector.
        if any(not " " <= character <= "~" for character in value):
            raise ValueError("HTTP_USER_AGENT must be printable ASCII")
        return value.strip()


class Config(BaseModel):
    app: AppConfig
    bot: BotConfig
    throttling: ThrottlingConfig
    web_server: WebServerConfig
    webhook: WebhookConfig
    postgres: PostgresConfig
    redis: RedisConfig
    cache: CacheConfig
    sentry: SentryConfig
    http: HttpConfig

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def user_cache_ttl_fits_the_version_ttl(self) -> Self:
        # Checked at startup: the cache refuses to write a value that outlives
        # its version counter, which would fail every update that caches a user.
        if self.bot.USER_CACHE_TTL_SECONDS > self.cache.CACHE_VERSION_TTL:
            raise ValueError("USER_CACHE_TTL_SECONDS must not exceed CACHE_VERSION_TTL")
        return self

    @model_validator(mode="after")
    def require_webhook_settings(self) -> Self:
        """Webhook mode cannot start without a public HTTPS URL and a secret.

        Checked here rather than on WebhookConfig because only Config sees
        BOT_MODE; polling mode leaves the webhook section unused, defaults and all.
        """
        if self.bot.BOT_MODE != "webhook":
            return self
        parsed = urlsplit(self.webhook.WEBHOOK_BASE_URL)
        if not parsed.netloc:
            raise ValueError("WEBHOOK_BASE_URL is required when BOT_MODE=webhook")
        if parsed.query or parsed.fragment:
            raise ValueError("WEBHOOK_BASE_URL must not carry a query or fragment")
        if parsed.scheme != "https":
            raise ValueError(
                "WEBHOOK_BASE_URL must use https: Telegram only calls HTTPS webhooks"
            )
        if not self.webhook.WEBHOOK_SECRET.get_secret_value():
            raise ValueError("WEBHOOK_SECRET is required when BOT_MODE=webhook")
        return self


@lru_cache
def get_settings() -> Config:
    """
    Cached settings factory. Override in tests via monkeypatching; a test that
    changes the environment must call `get_settings.cache_clear()`.

    The env file is chosen per call rather than pinned on the models, so that
    flipping TESTING (and clearing the cache) reloads from the other file.
    Environment variables win over the file, as they must in a deploy.
    """
    env_file = ".env.test" if os.getenv("TESTING") == "true" else ".env"
    return Config(
        app=AppConfig(_env_file=env_file),
        bot=BotConfig(_env_file=env_file),
        throttling=ThrottlingConfig(_env_file=env_file),
        web_server=WebServerConfig(_env_file=env_file),
        webhook=WebhookConfig(_env_file=env_file),
        postgres=PostgresConfig(_env_file=env_file),
        redis=RedisConfig(_env_file=env_file),
        cache=CacheConfig(_env_file=env_file),
        sentry=SentryConfig(_env_file=env_file),
        http=HttpConfig(_env_file=env_file),
    )
