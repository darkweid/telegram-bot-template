from pydantic import ValidationError
import pytest
from redis.connection import parse_url
from sqlalchemy.engine import make_url

from src.main.config import (
    BotConfig,
    Config,
    PostgresConfig,
    RedisConfig,
    WebhookConfig,
    WebServerConfig,
    get_settings,
)

VALID_TOKEN = "123456789:AAtest-bot-token-not-real-0000000000000"
VALID_SECRET = "s" * 32


def _bot(**overrides: object) -> BotConfig:
    return BotConfig(_env_file=None, BOT_TOKEN=VALID_TOKEN, **overrides)  # type: ignore[arg-type]


def _webhook(**overrides: object) -> WebhookConfig:
    return WebhookConfig(_env_file=None, **overrides)  # type: ignore[arg-type]


def _postgres(**overrides: object) -> PostgresConfig:
    values: dict[str, object] = {
        "DB_ECHO": False,
        "POSTGRES_USER": "app",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_HOST": "db-host",
        "POSTGRES_PORT": 5432,
        "POSTGRES_DB": "app",
    }
    values.update(overrides)
    return PostgresConfig(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "123:short",
        "abc:AAtest-bot-token-not-real-0000000000000",
        "123456789: spaced",
    ],
)
def test_bot_token_must_look_like_a_telegram_token(token: str) -> None:
    with pytest.raises(ValidationError, match="BOT_TOKEN"):
        BotConfig(_env_file=None, BOT_TOKEN=token)  # type: ignore[arg-type]


def test_bot_token_error_does_not_echo_the_token() -> None:
    with pytest.raises(ValidationError) as excinfo:
        BotConfig(_env_file=None, BOT_TOKEN="123:leaky")  # type: ignore[arg-type]
    assert "leaky" not in str(excinfo.value)


def test_default_locale_must_be_supported() -> None:
    with pytest.raises(ValidationError, match="DEFAULT_LOCALE"):
        _bot(DEFAULT_LOCALE="de")


def test_default_locale_is_normalized() -> None:
    assert _bot(DEFAULT_LOCALE="RU").DEFAULT_LOCALE == "ru"


@pytest.mark.parametrize(
    "path",
    [
        "webhook",
        "/",
        "//",
        "/a//b",
        "/-",
        "/webhook/",
        "/a/--",
        "/web hook",
        "/webhook?x=1",
        "/" + "a" * 256,
    ],
)
def test_webhook_path_is_a_plain_absolute_path(path: str) -> None:
    with pytest.raises(ValidationError, match="WEBHOOK_PATH"):
        _webhook(WEBHOOK_PATH=path)


def test_webhook_path_accepts_word_segments_with_hyphens() -> None:
    assert _webhook(WEBHOOK_PATH="/a/b-c/d_e").WEBHOOK_PATH == "/a/b-c/d_e"


@pytest.mark.parametrize(
    "secret", ["short", "x" * 257, "has space" + "x" * 32, "ünïcode" + "x" * 32]
)
def test_webhook_secret_follows_telegram_charset(secret: str) -> None:
    with pytest.raises(ValidationError, match="WEBHOOK_SECRET"):
        _webhook(WEBHOOK_SECRET=secret)


def test_webhook_url_joins_base_and_path() -> None:
    webhook = _webhook(
        WEBHOOK_BASE_URL="https://bot.example.com/", WEBHOOK_PATH="/webhook/tg"
    )
    assert webhook.url == "https://bot.example.com/webhook/tg"


def _config(**sections: object) -> Config:
    settings = get_settings()
    return settings.model_copy(update=sections)


def test_polling_mode_needs_no_webhook_settings() -> None:
    config = Config.model_validate(
        _config(bot=_bot(BOT_MODE="polling"), webhook=_webhook()).model_dump()
    )
    assert config.bot.BOT_MODE == "polling"


@pytest.mark.parametrize(
    ("webhook_values", "message"),
    [
        # Matched on the validator's own words: the error text also echoes the
        # input, which holds every field name, so a bare name would match anything.
        (
            {"WEBHOOK_SECRET": VALID_SECRET},
            "WEBHOOK_BASE_URL is required when BOT_MODE=webhook",
        ),
        (
            {
                "WEBHOOK_BASE_URL": "http://bot.example.com",
                "WEBHOOK_SECRET": VALID_SECRET,
            },
            "WEBHOOK_BASE_URL must use https",
        ),
        (
            {"WEBHOOK_BASE_URL": "https://", "WEBHOOK_SECRET": VALID_SECRET},
            "WEBHOOK_BASE_URL is required when BOT_MODE=webhook",
        ),
        (
            {
                "WEBHOOK_BASE_URL": "https://bot.example.com/?x=1",
                "WEBHOOK_SECRET": VALID_SECRET,
            },
            "WEBHOOK_BASE_URL must not carry a query or fragment",
        ),
        (
            {
                "WEBHOOK_BASE_URL": "https://bot.example.com/#frag",
                "WEBHOOK_SECRET": VALID_SECRET,
            },
            "WEBHOOK_BASE_URL must not carry a query or fragment",
        ),
        (
            {"WEBHOOK_BASE_URL": "https://bot.example.com"},
            "WEBHOOK_SECRET is required when BOT_MODE=webhook",
        ),
    ],
)
def test_webhook_mode_requires_a_complete_https_webhook(
    webhook_values: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Config.model_validate(
            _config(
                bot=_bot(BOT_MODE="webhook"), webhook=_webhook(**webhook_values)
            ).model_dump()
        )


def test_webhook_mode_accepts_a_complete_webhook() -> None:
    config = Config.model_validate(
        _config(
            bot=_bot(BOT_MODE="webhook"),
            webhook=_webhook(
                WEBHOOK_BASE_URL="https://bot.example.com", WEBHOOK_SECRET=VALID_SECRET
            ),
        ).model_dump()
    )
    assert config.webhook.url == "https://bot.example.com/webhook/telegram"


def test_redis_fsm_dsn_uses_its_own_database() -> None:
    redis = get_settings().redis.model_copy(update={"REDIS_FSM_DATABASE": "7"})
    assert redis.fsm_dsn.endswith("/7")
    assert redis.dsn != redis.fsm_dsn


def test_redis_dsn_escapes_special_characters_in_password() -> None:
    raw_password = "p@ss/word%:x"
    redis_config = RedisConfig(
        REDIS_HOST="redis-host",
        REDIS_PORT=6379,
        REDIS_PASSWORD=raw_password,
        REDIS_DATABASE="0",
    )

    for dsn, database in ((redis_config.dsn, 0), (redis_config.fsm_dsn, 2)):
        parsed = parse_url(dsn)
        assert parsed["password"] == raw_password
        assert parsed["host"] == "redis-host"
        assert parsed["db"] == database


def test_postgres_dsn_escapes_special_characters_in_credentials() -> None:
    raw_password = "p@ss/word%:x"
    postgres_config = _postgres(
        POSTGRES_USER="app:user", POSTGRES_PASSWORD=raw_password
    )

    for dsn in (postgres_config.dsn_async, postgres_config.dsn_sync):
        url = make_url(dsn)
        assert url.username == "app:user"
        assert url.password == raw_password
        assert url.host == "db-host"
        assert url.database == "app"


def test_postgres_rejects_question_mark_in_database_name() -> None:
    # make_url truncates the database at '?' (query string) and does not decode
    # the path segment, so the name can be neither passed raw nor encoded.
    with pytest.raises(ValidationError, match="POSTGRES_DB"):
        _postgres(POSTGRES_DB="app?x")


@pytest.mark.parametrize("odd_name", ["app/db", "app#1", "a@b", "a:b", "a b"])
def test_postgres_allows_database_names_make_url_preserves(odd_name: str) -> None:
    postgres_config = _postgres(POSTGRES_DB=odd_name)

    for dsn in (postgres_config.dsn_async, postgres_config.dsn_sync):
        assert make_url(dsn).database == odd_name


def test_postgres_pool_sizes_default_and_validate() -> None:
    postgres_config = _postgres()

    assert postgres_config.DB_POOL_SIZE == 5
    assert postgres_config.DB_MAX_OVERFLOW == 2

    with pytest.raises(ValidationError):
        _postgres(DB_POOL_SIZE=0)


@pytest.mark.parametrize(
    ("section", "values", "field"),
    [
        (WebServerConfig, {"WEB_SERVER_PORT": 0}, "WEB_SERVER_PORT"),
        (WebServerConfig, {"WEB_SERVER_PORT": 65536}, "WEB_SERVER_PORT"),
        (WebhookConfig, {"WEBHOOK_MAX_CONNECTIONS": 0}, "WEBHOOK_MAX_CONNECTIONS"),
        (WebhookConfig, {"WEBHOOK_MAX_CONNECTIONS": 101}, "WEBHOOK_MAX_CONNECTIONS"),
        (
            BotConfig,
            {"BOT_TOKEN": VALID_TOKEN, "TELEGRAM_RETRY_ATTEMPTS": 11},
            "TELEGRAM_RETRY_ATTEMPTS",
        ),
        (
            BotConfig,
            {"BOT_TOKEN": VALID_TOKEN, "TELEGRAM_RETRY_ATTEMPTS": -1},
            "TELEGRAM_RETRY_ATTEMPTS",
        ),
        (
            BotConfig,
            {"BOT_TOKEN": VALID_TOKEN, "USER_TOUCH_INTERVAL_SECONDS": 0},
            "USER_TOUCH_INTERVAL_SECONDS",
        ),
        (
            BotConfig,
            {"BOT_TOKEN": VALID_TOKEN, "SHUTDOWN_TIMEOUT_SECONDS": 0},
            "SHUTDOWN_TIMEOUT_SECONDS",
        ),
        (
            BotConfig,
            {"BOT_TOKEN": VALID_TOKEN, "BOT_MODE": "longpoll"},
            "BOT_MODE",
        ),
    ],
)
def test_numeric_bounds_and_bot_mode_are_enforced(
    section: type[BotConfig | WebServerConfig | WebhookConfig],
    values: dict[str, object],
    field: str,
) -> None:
    with pytest.raises(ValidationError, match=field):
        section(_env_file=None, **values)  # type: ignore[arg-type]


def test_get_settings_loads_the_test_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESTING", "true")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.app.TESTING is True
        assert settings.app.PROJECT_NAME == "Telegram bot test"
        assert settings.postgres.POSTGRES_DB == "test_db"
    finally:
        get_settings.cache_clear()
