from pathlib import Path

from dotenv import dotenv_values
from pydantic import ValidationError
import pytest
from redis.connection import parse_url
from sqlalchemy.engine import make_url

from src.main.config import (
    AppConfig,
    BotConfig,
    BroadcastConfig,
    CacheConfig,
    Config,
    HttpConfig,
    PostgresConfig,
    RedisConfig,
    WebhookConfig,
    WebServerConfig,
    get_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
VALID_TOKEN = "123456789:AAtest-bot-token-not-real-0000000000000"
VALID_SECRET = "s" * 32


def _bot(**overrides: object) -> BotConfig:
    return BotConfig(_env_file=None, BOT_TOKEN=VALID_TOKEN, **overrides)


def _webhook(**overrides: object) -> WebhookConfig:
    return WebhookConfig(_env_file=None, **overrides)


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
    return PostgresConfig(_env_file=None, **values)


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
        BotConfig(_env_file=None, BOT_TOKEN=token)


def test_bot_token_error_does_not_echo_the_token() -> None:
    with pytest.raises(ValidationError) as excinfo:
        BotConfig(_env_file=None, BOT_TOKEN="123:leaky")
    assert "leaky" not in str(excinfo.value)


def test_default_locale_must_be_supported() -> None:
    with pytest.raises(ValidationError, match="DEFAULT_LOCALE"):
        _bot(DEFAULT_LOCALE="de")


def test_default_locale_is_normalized() -> None:
    assert _bot(DEFAULT_LOCALE="RU").DEFAULT_LOCALE == "ru"


def test_admins_hear_of_new_users_by_default() -> None:
    assert _bot().NOTIFY_ADMINS_ON_NEW_USER is True


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


def test_each_redis_role_has_its_own_database(settings: Config) -> None:
    redis = settings.redis
    databases = {
        redis.REDIS_DATABASE,
        redis.REDIS_FSM_DATABASE,
        redis.REDIS_TASKS_DATABASE,
    }
    assert len(databases) == 3
    assert redis.tasks_dsn.endswith(f"/{redis.REDIS_TASKS_DATABASE}")


@pytest.mark.parametrize(
    ("databases", "message"),
    [
        (
            {"REDIS_DATABASE": "1"},
            "REDIS_DATABASE and REDIS_TASKS_DATABASE must differ, both are 1",
        ),
        (
            {"REDIS_DATABASE": "2"},
            "REDIS_DATABASE and REDIS_FSM_DATABASE must differ, both are 2",
        ),
        (
            {"REDIS_DATABASE": "0", "REDIS_TASKS_DATABASE": "2"},
            "REDIS_FSM_DATABASE and REDIS_TASKS_DATABASE must differ, both are 2",
        ),
    ],
)
def test_redis_roles_on_one_database_are_refused(
    databases: dict[str, str], message: str
) -> None:
    """A cache flush on a shared database would empty the task queue or every
    user's dialog state with it."""
    values = {"REDIS_HOST": "redis-host", "REDIS_PORT": 6379, "REDIS_PASSWORD": "x"}
    with pytest.raises(ValidationError, match=message):
        RedisConfig(**values, **databases)


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


_REQUIRED: dict[type, dict[str, object]] = {
    RedisConfig: {
        "REDIS_HOST": "redis-host",
        "REDIS_PASSWORD": "secret",
        "REDIS_DATABASE": "0",
        "REDIS_PORT": 6379,
    },
    PostgresConfig: {
        "DB_ECHO": False,
        "POSTGRES_USER": "app",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_HOST": "db-host",
        "POSTGRES_PORT": 5432,
        "POSTGRES_DB": "app",
    },
}


@pytest.mark.parametrize(
    ("section", "values", "field"),
    [
        (WebServerConfig, {"WEB_SERVER_PORT": 0}, "WEB_SERVER_PORT"),
        (WebServerConfig, {"WEB_SERVER_PORT": 65536}, "WEB_SERVER_PORT"),
        (RedisConfig, {"REDIS_PORT": 0}, "REDIS_PORT"),
        (RedisConfig, {"REDIS_PORT": 65536}, "REDIS_PORT"),
        (PostgresConfig, {"POSTGRES_PORT": 0}, "POSTGRES_PORT"),
        (PostgresConfig, {"POSTGRES_PORT": 65536}, "POSTGRES_PORT"),
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
            {"BOT_TOKEN": VALID_TOKEN, "ALERT_COOLDOWN_SECONDS": 0},
            "ALERT_COOLDOWN_SECONDS",
        ),
        (
            BotConfig,
            {"BOT_TOKEN": VALID_TOKEN, "DEV_ALERT_CHAT_ID": 0},
            "DEV_ALERT_CHAT_ID",
        ),
        (
            BotConfig,
            {"BOT_TOKEN": VALID_TOKEN, "BOT_MODE": "longpoll"},
            "BOT_MODE",
        ),
    ],
)
def test_numeric_bounds_and_bot_mode_are_enforced(
    section: type[
        BotConfig | WebServerConfig | WebhookConfig | RedisConfig | PostgresConfig
    ],
    values: dict[str, object],
    field: str,
) -> None:
    with pytest.raises(ValidationError, match=field):
        kwargs = {**_REQUIRED.get(section, {}), **values}
        section(_env_file=None, **kwargs)


@pytest.mark.parametrize("port", [1, 65535])
def test_port_bounds_accept_their_edges(port: int) -> None:
    assert WebServerConfig(_env_file=None, WEB_SERVER_PORT=port).WEB_SERVER_PORT == port
    redis = RedisConfig(
        _env_file=None, **{**_REQUIRED[RedisConfig], "REDIS_PORT": port}
    )
    assert redis.REDIS_PORT == port
    assert _postgres(POSTGRES_PORT=port).POSTGRES_PORT == port


def test_an_image_without_a_commit_says_unknown() -> None:
    """A local build outside git must still start; /status shows the gap."""
    config = AppConfig(_env_file=None, PROJECT_NAME="Bot", VERSION="1.0.0")
    assert config.COMMIT_SHA == "unknown"


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


def test_env_example_token_is_a_placeholder_the_validator_rejects() -> None:
    """A copied .env.example must stop at startup until the token is replaced,
    instead of starting a bot that fails every call against Telegram."""
    values = dotenv_values(PROJECT_ROOT / ".env.example")

    assert values.get("BOT_TOKEN")
    with pytest.raises(ValidationError, match="does not look like a token"):
        BotConfig(_env_file=None, BOT_TOKEN=values["BOT_TOKEN"])


def test_role_ids_are_parsed_from_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose and .env pass lists as plain strings; a JSON list is easy to
    get wrong in an env file and fails with an unreadable error."""
    monkeypatch.setenv("ADMIN_IDS", " 11, 22 ,")
    monkeypatch.setenv("DEVELOPER_IDS", "33")
    config = _bot()
    assert config.ADMIN_IDS == frozenset({11, 22})
    assert config.DEVELOPER_IDS == frozenset({33})


def test_role_ids_default_to_nobody() -> None:
    config = _bot()
    assert config.ADMIN_IDS == frozenset()
    assert config.DEVELOPER_IDS == frozenset()


@pytest.mark.parametrize("key", ["ADMIN_IDS", "DEVELOPER_IDS"])
@pytest.mark.parametrize("value", ["abc", "0", "-100123", "1;2"])
def test_role_ids_reject_anything_but_user_ids(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    """A group id or a typo would silently grant nobody the role."""
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        _bot()


def test_role_ids_reject_a_group_id_passed_as_a_collection() -> None:
    """Positivity must not depend on how the value arrives, or a group id slips
    in through a code path other than the environment."""
    with pytest.raises(ValidationError):
        _bot(ADMIN_IDS=frozenset({-100123}))


def test_an_empty_alert_chat_means_private_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEV_ALERT_CHAT_ID", "")
    assert _bot().DEV_ALERT_CHAT_ID is None


def test_an_alert_chat_id_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_ALERT_CHAT_ID", "-1001234567890")
    assert _bot().DEV_ALERT_CHAT_ID == -1001234567890


@pytest.mark.parametrize(
    "value", ["bot/1\r\nX-Evil: 1", "bot/1\x00", "бот/1", "x" * 257]
)
def test_a_user_agent_that_could_split_a_request_is_refused(value: str) -> None:
    """A control character in a header is a request-splitting vector."""
    with pytest.raises(ValidationError):
        HttpConfig(HTTP_USER_AGENT=value)


def test_cache_default_ttl_may_not_outlive_the_version_counter() -> None:
    """A default TTL above the counter's would raise on every write that does
    not set its own TTL, failing each update that touches the cache."""
    with pytest.raises(ValidationError, match="CACHE_DEFAULT_TTL"):
        CacheConfig(_env_file=None, CACHE_DEFAULT_TTL=100, CACHE_VERSION_TTL=99)
    assert (
        CacheConfig(
            _env_file=None, CACHE_DEFAULT_TTL=100, CACHE_VERSION_TTL=100
        ).CACHE_DEFAULT_TTL
        == 100
    )


def test_throttling_defaults_match_the_spec(settings: Config) -> None:
    """The README and .env.example quote these numbers; a drifted default would
    make both wrong."""
    throttling = settings.throttling
    assert (
        throttling.THROTTLE_DEFAULT_LIMIT,
        throttling.THROTTLE_DEFAULT_WINDOW_SECONDS,
    ) == (20, 60)
    assert (throttling.THROTTLE_LLM_LIMIT, throttling.THROTTLE_LLM_WINDOW_SECONDS) == (
        5,
        60,
    )


def test_user_cache_ttl_may_not_outlive_the_version_counter() -> None:
    """The cache refuses to write a value that outlives its version counter,
    which would fail every update that caches a user."""
    cache = get_settings().cache.model_copy(update={"CACHE_VERSION_TTL": 100})
    with pytest.raises(ValidationError, match="USER_CACHE_TTL_SECONDS"):
        Config.model_validate(
            _config(bot=_bot(USER_CACHE_TTL_SECONDS=101), cache=cache).model_dump()
        )
    config = Config.model_validate(
        _config(bot=_bot(USER_CACHE_TTL_SECONDS=100), cache=cache).model_dump()
    )
    assert config.bot.USER_CACHE_TTL_SECONDS == 100


def test_broadcast_defaults_stay_under_the_bot_api_limit() -> None:
    """Telegram allows about 30 messages a second to different users; the
    default leaves headroom for the bot's other traffic."""
    config = BroadcastConfig()
    assert config.BROADCAST_RATE_PER_SECOND == 25
    assert config.BROADCAST_CHECKPOINT_EVERY == 50


@pytest.mark.parametrize(
    "values",
    [
        {"BROADCAST_RATE_PER_SECOND": 0},
        {"BROADCAST_RATE_PER_SECOND": 31},
        {"BROADCAST_CHECKPOINT_EVERY": 0},
    ],
)
def test_broadcast_settings_out_of_range_are_refused(values: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        BroadcastConfig(**values)
