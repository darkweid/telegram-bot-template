from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError
import pytest

from src.core.llm.enums import LLMProviderName
from src.main.config import Config, LLMConfig

PROJECT_ROOT = Path(__file__).resolve().parents[4]

ANTHROPIC_KEY = SecretStr("sk-ant-test-not-real")
OPENROUTER_KEY = SecretStr("sk-or-test-not-real")


def _llm(**values: object) -> LLMConfig:
    # _env_file=None: only the values given here and the environment, which
    # tests/unit/conftest.py strips of every config key.
    return LLMConfig(_env_file=None, **values)  # type: ignore[arg-type]


def test_llm_is_off_without_any_setting() -> None:
    """A bot started from the template must run before anyone buys a key."""
    config = _llm()

    assert config.LLM_PROVIDER is None
    assert config.LLM_FALLBACK_PROVIDER is None


def test_an_empty_provider_in_env_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LLM_PROVIDER=` is how .env switches the layer off; read as an
    invalid provider name it would stop the bot instead."""
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "")

    assert _llm().LLM_PROVIDER is None


def test_the_test_env_keeps_llm_off(settings: Config) -> None:
    """A test that reached a real provider would spend money and depend on
    the network."""
    assert settings.llm.LLM_PROVIDER is None


@pytest.mark.parametrize(
    ("provider", "key_setting"),
    [
        (LLMProviderName.ANTHROPIC, "ANTHROPIC_API_KEY"),
        (LLMProviderName.OPENROUTER, "OPENROUTER_API_KEY"),
    ],
)
def test_a_provider_without_its_key_stops_startup(
    provider: LLMProviderName, key_setting: str
) -> None:
    """Without the check every call would fail with a 401 at run time,
    long after the deploy looked healthy."""
    model = (
        "google/gemini-3.8-flash" if provider == "openrouter" else "claude-sonnet-5-5"
    )

    with pytest.raises(ValidationError, match=key_setting):
        _llm(LLM_PROVIDER=provider, LLM_MODEL=model)


def test_a_provider_without_a_model_stops_startup() -> None:
    with pytest.raises(ValidationError, match="LLM_MODEL is required"):
        _llm(LLM_PROVIDER="anthropic", LLM_MODEL="", ANTHROPIC_API_KEY=ANTHROPIC_KEY)


def test_a_fallback_without_a_primary_is_refused() -> None:
    """A fallback alone would read as "LLM on" while every call raises
    LLMNotConfigured."""
    with pytest.raises(ValidationError, match="nothing to fall back from"):
        _llm(
            LLM_FALLBACK_PROVIDER="openrouter",
            LLM_FALLBACK_MODEL="google/gemini-3.8-flash",
            OPENROUTER_API_KEY=OPENROUTER_KEY,
        )


def test_a_fallback_on_the_primary_provider_is_refused() -> None:
    """The fallback exists for the primary's outage; the same provider
    would be down at the same moment."""
    with pytest.raises(ValidationError, match="must differ from LLM_PROVIDER"):
        _llm(
            LLM_PROVIDER="anthropic",
            ANTHROPIC_API_KEY=ANTHROPIC_KEY,
            LLM_FALLBACK_PROVIDER="anthropic",
            LLM_FALLBACK_MODEL="claude-haiku-5-5",
        )


def test_a_fallback_needs_its_own_key_and_model() -> None:
    with pytest.raises(ValidationError, match="OPENROUTER_API_KEY"):
        _llm(
            LLM_PROVIDER="anthropic",
            ANTHROPIC_API_KEY=ANTHROPIC_KEY,
            LLM_FALLBACK_PROVIDER="openrouter",
            LLM_FALLBACK_MODEL="google/gemini-3.8-flash",
        )
    with pytest.raises(ValidationError, match="LLM_FALLBACK_MODEL is required"):
        _llm(
            LLM_PROVIDER="anthropic",
            ANTHROPIC_API_KEY=ANTHROPIC_KEY,
            LLM_FALLBACK_PROVIDER="openrouter",
            OPENROUTER_API_KEY=OPENROUTER_KEY,
        )


@pytest.mark.parametrize(
    ("provider", "model", "message"),
    [
        ("openrouter", "gemini-3.8-flash", "<vendor>/<model>"),
        ("anthropic", "anthropic/claude-sonnet-5-5", "carry no vendor prefix"),
    ],
)
def test_a_model_id_of_the_other_providers_shape_is_refused(
    provider: str, model: str, message: str
) -> None:
    """Both providers answer an unknown model with an error on every call;
    the id's shape catches the common mix-up at startup."""
    with pytest.raises(ValidationError, match=message):
        _llm(
            LLM_PROVIDER=provider,
            LLM_MODEL=model,
            ANTHROPIC_API_KEY=ANTHROPIC_KEY,
            OPENROUTER_API_KEY=OPENROUTER_KEY,
        )


def test_a_validation_error_never_echoes_a_key() -> None:
    """The startup error goes to the log and to Sentry; the key must not."""
    with pytest.raises(ValidationError) as excinfo:
        _llm(
            LLM_PROVIDER="anthropic",
            LLM_MODEL="vendor/model",
            ANTHROPIC_API_KEY="sk-ant-leaky-value",
        )

    assert "sk-ant-leaky-value" not in str(excinfo.value)


def test_a_full_two_provider_setup_is_accepted() -> None:
    config = _llm(
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY=ANTHROPIC_KEY,
        LLM_FALLBACK_PROVIDER="openrouter",
        LLM_FALLBACK_MODEL="google/gemini-3.8-flash",
        OPENROUTER_API_KEY=OPENROUTER_KEY,
    )

    assert config.LLM_PROVIDER is LLMProviderName.ANTHROPIC
    assert config.LLM_FALLBACK_PROVIDER is LLMProviderName.OPENROUTER


def test_env_example_passes_validation() -> None:
    """CI copies .env.example to .env and starts alembic from it; a value
    there that fails validation breaks every pipeline."""
    values = {
        key: value
        for key, value in dotenv_values(PROJECT_ROOT / ".env.example").items()
        if key in LLMConfig.model_fields and value is not None
    }

    config = _llm(**values)

    assert config.LLM_PROVIDER is LLMProviderName.ANTHROPIC
    assert config.LLM_FALLBACK_MODEL == "google/gemini-3.8-flash"


@pytest.mark.parametrize("setting", ["OPENROUTER_APP_URL", "OPENROUTER_APP_TITLE"])
def test_openrouter_headers_refuse_control_characters(setting: str) -> None:
    """Both values go out as HTTP headers; a newline there splits the request."""
    with pytest.raises(ValidationError, match="printable ASCII"):
        _llm(**{setting: "bot\r\nX-Injected: 1"})


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("LLM_TIMEOUT_SECONDS", 0),
        ("LLM_TIMEOUT_SECONDS", 601),
        ("LLM_MAX_RETRIES", -1),
        ("LLM_MAX_RETRIES", 6),
        ("LLM_USAGE_RETENTION_DAYS", 29),
    ],
)
def test_numeric_settings_outside_their_range_stop_startup(
    setting: str, value: int
) -> None:
    """A zero timeout fails every call; a retention under 30 days cuts the
    30-day figures of /stats short."""
    with pytest.raises(ValidationError, match=setting):
        _llm(**{setting: value})
