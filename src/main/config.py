from functools import lru_cache
import json
import os
import re
from typing import Annotated, Any, Literal, Self, assert_never
from urllib.parse import quote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.core.i18n import SUPPORTED_LOCALES
from src.core.llm.enums import LLMProviderName
from src.core.llm.pricing import ModelPrice


class RedisConfig(BaseSettings):
    REDIS_HOST: str
    REDIS_PORT: int = Field(ge=1, le=65535)
    REDIS_PASSWORD: str
    REDIS_DATABASE: int = Field(ge=0)
    REDIS_FSM_DATABASE: int = Field(2, ge=0)
    REDIS_TASKS_DATABASE: int = Field(1, ge=0)

    model_config = SettingsConfigDict(extra="ignore")

    @model_validator(mode="after")
    def each_role_has_its_own_database(self) -> Self:
        # Two roles on one database mix their keys: a cache flush takes the
        # task queue or the FSM state with it. Compared as numbers, so "01"
        # and "1" are caught as the one database they are.
        databases = {
            "REDIS_DATABASE": self.REDIS_DATABASE,
            "REDIS_FSM_DATABASE": self.REDIS_FSM_DATABASE,
            "REDIS_TASKS_DATABASE": self.REDIS_TASKS_DATABASE,
        }
        names = list(databases)
        for index, name in enumerate(names):
            for other in names[index + 1 :]:
                if databases[name] == databases[other]:
                    raise ValueError(
                        f"{name} and {other} must differ, both are {databases[name]}"
                    )
        return self

    def _dsn(self, database: int) -> str:
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
    # Baked into the image at build time (Dockerfile ARG, set by make and CI);
    # shown by /status and /health.
    COMMIT_SHA: str = "unknown"
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
    # Where developer alerts go: a group or channel id. Empty sends each alert
    # to every DEVELOPER_IDS member in private.
    DEV_ALERT_CHAT_ID: int | None = None
    # The same failure (type, origin, place) is alerted once per this many seconds.
    ALERT_COOLDOWN_SECONDS: int = Field(300, gt=0)
    # Message every admin when a user starts the bot for the first time. Off,
    # the notice task still runs and sends nothing.
    NOTIFY_ADMINS_ON_NEW_USER: bool = True

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    @field_validator("DEV_ALERT_CHAT_ID", mode="before")
    @classmethod
    def empty_alert_chat_is_none(cls, value: object) -> object:
        # `DEV_ALERT_CHAT_ID=` in .env means "not set", not an invalid int.
        return None if value == "" else value

    @field_validator("DEV_ALERT_CHAT_ID")
    @classmethod
    def require_an_alert_chat(cls, value: int | None) -> int | None:
        # 0 is no chat: Telegram would refuse every alert, and they are only logged.
        if value == 0:
            raise ValueError("DEV_ALERT_CHAT_ID must be a chat id or empty, not 0")
        return value

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


class BroadcastConfig(BaseSettings):
    # Copies a broadcast sends per second. Telegram allows about 30 messages a
    # second to different users; closer to it every copy meets flood control.
    BROADCAST_RATE_PER_SECOND: float = Field(25, gt=0, le=30)
    # Recipients per batch and checkpoint. A worker restart may send up to this
    # many messages a second time; a large value also makes a stop slower.
    BROADCAST_CHECKPOINT_EVERY: int = Field(50, ge=1, le=1000)

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


def _printable_header(value: str, setting: str) -> str:
    # A control character in a header value is a request-splitting vector.
    if any(not " " <= character <= "~" for character in value):
        raise ValueError(f"{setting} must be printable ASCII")
    return value.strip()


class HttpConfig(BaseSettings):
    # Sent on every outgoing request (src/core/http). Blank derives
    # "<project-name>/<VERSION>" from PROJECT_NAME and VERSION. Many APIs
    # behind a WAF refuse the library's default agent outright.
    HTTP_USER_AGENT: str = Field("", max_length=256)

    model_config = SettingsConfigDict(extra="ignore")

    @field_validator("HTTP_USER_AGENT")
    @classmethod
    def reject_unprintable(cls, value: str) -> str:
        return _printable_header(value, "HTTP_USER_AGENT")


class LLMConfig(BaseSettings):
    # Empty turns the LLM layer off: the bot starts without any key and every
    # call raises LLMNotConfigured. Set, it requires that provider's key.
    LLM_PROVIDER: LLMProviderName | None = None
    # Anthropic ids as they are; OpenRouter ids are "<vendor>/<model>".
    LLM_MODEL: str = "claude-sonnet-5-5"
    # Takes over when the primary is unavailable, times out or is rate limited.
    LLM_FALLBACK_PROVIDER: LLMProviderName | None = None
    LLM_FALLBACK_MODEL: str = ""
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    OPENROUTER_API_KEY: SecretStr = SecretStr("")
    # OpenRouter's attribution headers (HTTP-Referer, X-Title); blank sends none.
    OPENROUTER_APP_URL: str = Field("", max_length=256)
    OPENROUTER_APP_TITLE: str = Field("", max_length=256)
    # Per attempt; the SDK retries 429, 5xx and network errors LLM_MAX_RETRIES
    # times, so a caller waits up to timeout x (retries + 1) per provider.
    LLM_TIMEOUT_SECONDS: float = Field(60, gt=0, le=600)
    LLM_MAX_RETRIES: int = Field(1, ge=0, le=5)
    # Replaces or adds entries of src/core/llm/pricing.py, as JSON:
    # {"<model>": {"input_per_mtok": "3", "output_per_mtok": "15", ...}}.
    # NoDecode: parsed below, so that an empty value means no overrides.
    LLM_PRICING_OVERRIDES: Annotated[dict[str, ModelPrice], NoDecode] = Field(
        default_factory=dict
    )
    # /stats reports up to 30 days back, so a shorter retention would leave
    # its 30-day figures short.
    LLM_USAGE_RETENTION_DAYS: int = Field(90, ge=30)
    # Logs prompts and answers in full: what users typed. Local debugging only.
    LLM_LOG_CONTENT: bool = False

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    @field_validator("LLM_PROVIDER", "LLM_FALLBACK_PROVIDER", mode="before")
    @classmethod
    def empty_provider_is_none(cls, value: object) -> object:
        # `LLM_PROVIDER=` in .env means "off", not an invalid provider name.
        return None if value == "" else value

    @field_validator("LLM_PRICING_OVERRIDES", mode="before")
    @classmethod
    def parse_pricing_overrides(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return json.loads(value) if value.strip() else {}

    @field_validator("OPENROUTER_APP_URL", "OPENROUTER_APP_TITLE")
    @classmethod
    def reject_unprintable(cls, value: str, info: ValidationInfo) -> str:
        # Both go out as HTTP header values, which are ASCII.
        return _printable_header(value, str(info.field_name))

    @model_validator(mode="after")
    def require_provider_settings(self) -> Self:
        """A configured provider must be callable at startup, not fail every
        call later: its key, a model id of its shape, and a fallback that does
        not share the primary's outage."""
        if self.LLM_PROVIDER is None:
            if self.LLM_FALLBACK_PROVIDER is not None:
                raise ValueError(
                    "LLM_FALLBACK_PROVIDER is set but LLM_PROVIDER is empty: "
                    "there is nothing to fall back from"
                )
            return self
        self._require_provider(self.LLM_PROVIDER, self.LLM_MODEL, "LLM_MODEL")
        if self.LLM_FALLBACK_PROVIDER is None:
            return self
        if self.LLM_FALLBACK_PROVIDER == self.LLM_PROVIDER:
            raise ValueError(
                "LLM_FALLBACK_PROVIDER must differ from LLM_PROVIDER: one "
                "provider's outage would stop both"
            )
        self._require_provider(
            self.LLM_FALLBACK_PROVIDER, self.LLM_FALLBACK_MODEL, "LLM_FALLBACK_MODEL"
        )
        return self

    def _require_provider(
        self, provider: LLMProviderName, model: str, model_setting: str
    ) -> None:
        match provider:
            case LLMProviderName.ANTHROPIC:
                key_setting, key = "ANTHROPIC_API_KEY", self.ANTHROPIC_API_KEY
            case LLMProviderName.OPENROUTER:
                key_setting, key = "OPENROUTER_API_KEY", self.OPENROUTER_API_KEY
            case _:
                assert_never(provider)
        if not key.get_secret_value():
            raise ValueError(f"{key_setting} is required when {provider} is used")
        if not model:
            raise ValueError(f"{model_setting} is required when {provider} is used")
        vendor_prefixed = "/" in model
        if provider is LLMProviderName.OPENROUTER and not vendor_prefixed:
            raise ValueError(
                f"{model_setting}={model}: OpenRouter model ids are "
                "<vendor>/<model>, e.g. google/gemini-3.8-flash"
            )
        if provider is LLMProviderName.ANTHROPIC and vendor_prefixed:
            raise ValueError(
                f"{model_setting}={model}: Anthropic model ids carry no vendor "
                "prefix, e.g. claude-sonnet-5-5"
            )


class S3Config(BaseSettings):
    # Off: the container holds a client that refuses every call with a clear
    # error, so a bot that stores no files needs none of the keys below.
    S3_ENABLED: bool = False
    S3_BUCKET_NAME: str = ""
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: SecretStr = SecretStr("")
    S3_REGION_NAME: str = ""
    S3_PRE_SIGNED_URL_SECONDS: int = Field(300, gt=0)
    S3_ENDPOINT_URL: str | None = None
    S3_ADDRESSING_STYLE: Literal["auto", "path", "virtual"] | None = None
    S3_SIGNATURE_VERSION: str = Field("s3v4", min_length=1)
    S3_VERIFY_SSL: bool = True
    S3_CA_BUNDLE: str | None = None
    S3_TREAT_ACCESS_DENIED_AS_MISSING: bool = False
    S3_CONNECT_TIMEOUT_SECONDS: int = Field(5, gt=0)
    S3_READ_TIMEOUT_SECONDS: int = Field(120, gt=0)
    S3_RETRY_MAX_ATTEMPTS: int = Field(3, gt=0)
    S3_RETRY_MODE: Literal["legacy", "standard", "adaptive"] = "standard"
    # 20 MiB: the most the Bot API lets a bot download with getFile.
    S3_MAX_UPLOAD_SIZE_BYTES: int = Field(20 * 1024 * 1024, gt=0)

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    @field_validator(
        "S3_ENDPOINT_URL", "S3_ADDRESSING_STYLE", "S3_CA_BUNDLE", mode="before"
    )
    @classmethod
    def blank_means_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def require_credentials_when_enabled(self) -> Self:
        if not self.S3_ENABLED:
            return self
        values = {
            "S3_BUCKET_NAME": self.S3_BUCKET_NAME,
            "S3_ACCESS_KEY_ID": self.S3_ACCESS_KEY_ID,
            "S3_SECRET_ACCESS_KEY": self.S3_SECRET_ACCESS_KEY.get_secret_value(),
            "S3_REGION_NAME": self.S3_REGION_NAME,
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"S3_ENABLED=true requires: {', '.join(missing)}")
        return self


class Config(BaseModel):
    app: AppConfig
    bot: BotConfig
    throttling: ThrottlingConfig
    broadcast: BroadcastConfig
    web_server: WebServerConfig
    webhook: WebhookConfig
    postgres: PostgresConfig
    redis: RedisConfig
    cache: CacheConfig
    sentry: SentryConfig
    http: HttpConfig
    llm: LLMConfig
    s3: S3Config

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
        broadcast=BroadcastConfig(_env_file=env_file),
        web_server=WebServerConfig(_env_file=env_file),
        webhook=WebhookConfig(_env_file=env_file),
        postgres=PostgresConfig(_env_file=env_file),
        redis=RedisConfig(_env_file=env_file),
        cache=CacheConfig(_env_file=env_file),
        sentry=SentryConfig(_env_file=env_file),
        http=HttpConfig(_env_file=env_file),
        llm=LLMConfig(_env_file=env_file),
        s3=S3Config(_env_file=env_file),
    )
