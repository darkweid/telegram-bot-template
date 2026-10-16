from decimal import Decimal
from pathlib import Path
import re

from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError
import pytest

from src.core.llm.enums import LLMProviderName
from src.core.llm.usage.pricing import PRICES, ModelPrice
from src.main.config import Config, LLMConfig

PROJECT_ROOT = Path(__file__).resolve().parents[4]
ENV_EXAMPLE = (PROJECT_ROOT / ".env.example").read_text()

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


def _commented_example(name: str) -> str | None:
    """The value of `# NAME=value` in .env.example: a setting shown, not set."""
    match = re.search(rf"^# {name}=(.+)$", ENV_EXAMPLE, re.MULTILINE)
    return None if match is None else match.group(1)


def test_env_example_passes_validation() -> None:
    """CI copies .env.example to .env and starts alembic from it; a value
    there that fails validation breaks every pipeline."""
    values = {
        key: value
        for key, value in dotenv_values(PROJECT_ROOT / ".env.example").items()
        if key in LLMConfig.model_fields and value is not None
    }

    config = _llm(**values)

    assert config.LLM_PROVIDER is None
    assert config.LLM_FALLBACK_PROVIDER is None


def test_env_example_ships_the_llm_off() -> None:
    """Placeholder keys would let the bot start and then fail every call at
    the provider, alerting developers each time; a copied .env starts with
    the layer off instead, like S3 and email."""
    example = dotenv_values(PROJECT_ROOT / ".env.example")

    assert example["LLM_PROVIDER"] == ""
    assert example["LLM_FALLBACK_PROVIDER"] == ""
    assert example["ANTHROPIC_API_KEY"] == ""
    assert example["OPENROUTER_API_KEY"] == ""


def test_the_commented_llm_example_passes_validation() -> None:
    """Uncommenting the example lines and pasting real keys is how a new bot
    turns the layer on; the example itself must be a valid configuration."""
    names = (
        "LLM_PROVIDER",
        "LLM_FALLBACK_PROVIDER",
        "LLM_FALLBACK_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "TRANSCRIBE_MODEL",
    )
    values = {name: _commented_example(name) for name in names}
    assert None not in values.values(), values

    config = _llm(**values)

    assert config.LLM_PROVIDER is LLMProviderName.ANTHROPIC
    assert config.LLM_FALLBACK_PROVIDER is LLMProviderName.OPENROUTER
    assert config.TRANSCRIBE_MODEL == "google/gemini-3.8-flash"


def test_a_transcribe_model_needs_the_openrouter_key() -> None:
    """Without the check every voice message would fail with a 401 at run time."""
    with pytest.raises(ValidationError, match="OPENROUTER_API_KEY"):
        _llm(TRANSCRIBE_MODEL="google/gemini-3.8-flash")


def test_a_transcribe_model_needs_a_vendor_prefix() -> None:
    """OpenRouter answers 400 to a bare model id; better to stop at startup."""
    with pytest.raises(ValidationError, match="TRANSCRIBE_MODEL"):
        _llm(TRANSCRIBE_MODEL="gemini-3.8-flash", OPENROUTER_API_KEY=OPENROUTER_KEY)


def test_transcribe_max_tokens_below_the_reasoning_floor_is_refused() -> None:
    """The model reasons before it answers; under 2000 tokens a transcript is
    cut off."""
    with pytest.raises(ValidationError, match="TRANSCRIBE_MAX_TOKENS"):
        _llm(TRANSCRIBE_MAX_TOKENS=1999)


def test_transcriber_works_beside_an_anthropic_llm() -> None:
    """The transcriber has its own provider, whatever the chat model is."""
    config = _llm(
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY=ANTHROPIC_KEY,
        OPENROUTER_API_KEY=OPENROUTER_KEY,
        TRANSCRIBE_MODEL="google/gemini-3.8-flash",
    )

    assert config.LLM_PROVIDER is LLMProviderName.ANTHROPIC
    assert config.TRANSCRIBE_MODEL == "google/gemini-3.8-flash"


def test_transcriber_works_with_the_llm_off() -> None:
    """A bot may transcribe voice without any chat model."""
    config = _llm(
        OPENROUTER_API_KEY=OPENROUTER_KEY, TRANSCRIBE_MODEL="google/gemini-3.8-flash"
    )

    assert config.LLM_PROVIDER is None


def test_the_openrouter_key_stays_out_of_a_transcriber_error() -> None:
    """A validation error ends up in logs; the key must not."""
    with pytest.raises(ValidationError) as error:
        _llm(
            TRANSCRIBE_MODEL="gemini",
            OPENROUTER_API_KEY=SecretStr("sk-or-secret-not-real"),
        )

    assert "sk-or-secret-not-real" not in str(error.value)


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


SONNET_OVERRIDE = (
    '{"claude-sonnet-5-5": {"input_per_mtok": "3", "output_per_mtok": "15",'
    ' "cache_write_per_mtok": "3.75", "cache_read_per_mtok": "0.30"}}'
)


def test_pricing_overrides_are_read_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """A price change must be applicable from .env alone."""
    monkeypatch.setenv("LLM_PRICING_OVERRIDES", SONNET_OVERRIDE)

    overrides = _llm().LLM_PRICING_OVERRIDES

    assert overrides["claude-sonnet-5-5"].input_per_mtok == Decimal("3")
    assert overrides["claude-sonnet-5-5"].cache_read_per_mtok == Decimal("0.30")


def test_empty_pricing_overrides_mean_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LLM_PRICING_OVERRIDES=` stands in .env.example; it must not stop
    the bot as invalid JSON."""
    monkeypatch.setenv("LLM_PRICING_OVERRIDES", "")

    assert _llm().LLM_PRICING_OVERRIDES == {}


@pytest.mark.parametrize(
    "value",
    [
        "{not json",
        '["claude-sonnet-5-5"]',
        '{"claude-sonnet-5-5": {"input_per_mtok": "3"}}',
        '{"claude-sonnet-5-5": {"input_per_mtok": "-3", "output_per_mtok": "15",'
        ' "cache_write_per_mtok": "3.75", "cache_read_per_mtok": "0.30"}}',
        '{"claude-sonnet-5-5": {"input_per_mtok": "3", "output_per_mtok": "15",'
        ' "cache_write_per_mtok": "3.75", "cache_read_per_mtok": "0.30",'
        ' "cache_write_1h_per_mtok": "6"}}',
    ],
    ids=["malformed", "not-an-object", "missing-rate", "negative", "unknown-rate"],
)
def test_a_bad_pricing_override_stops_startup(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A price typo found at startup costs a restart; found in /stats a
    month later it costs a month of wrong figures."""
    monkeypatch.setenv("LLM_PRICING_OVERRIDES", value)

    with pytest.raises(ValidationError, match="LLM_PRICING_OVERRIDES"):
        _llm()


def test_the_documented_override_example_parses(tmp_path: Path) -> None:
    """The commented example in .env.example is what a developer copies;
    it must work once uncommented."""
    example = next(
        line.removeprefix("# ")
        for line in (PROJECT_ROOT / ".env.example").read_text().splitlines()
        if line.startswith("# LLM_PRICING_OVERRIDES=")
    )
    env_file = tmp_path / ".env"
    env_file.write_text(example + "\n")

    overrides = LLMConfig(_env_file=env_file).LLM_PRICING_OVERRIDES

    assert set(overrides) == {"claude-opus-5-5"}
    assert isinstance(overrides["claude-opus-5-5"], ModelPrice)


def test_every_shipped_anthropic_model_has_a_price() -> None:
    """A model without a price records its calls with no cost and /stats
    shows nothing spent; the defaults a new bot starts with, and the models
    .env.example shows, must be priced."""
    shown = re.findall(
        r"^(?:# )?LLM_(?:FALLBACK_)?MODEL(?:_FAST)?=(.+)$", ENV_EXAMPLE, re.MULTILINE
    )
    # OpenRouter ids are "<vendor>/<model>" (LLMConfig enforces it), so an id
    # without a vendor is an Anthropic one.
    anthropic_models = {
        model
        for model in [
            LLMConfig.model_fields["LLM_MODEL"].default,
            LLMConfig.model_fields["LLM_MODEL_FAST"].default,
            *shown,
        ]
        if model and "/" not in model
    }

    assert anthropic_models
    assert anthropic_models <= set(PRICES)


def test_an_empty_fast_model_is_allowed() -> None:
    """A bot with one model leaves the fast tier empty and gets the strong one."""
    config = LLMConfig(
        _env_file=None,
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="sk-ant-not-real",
        LLM_MODEL_FAST="",
    )
    assert config.LLM_MODEL_FAST == ""


@pytest.mark.parametrize(
    ("values", "key"),
    [
        (
            {
                "LLM_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "sk-or-not-real",
                "LLM_MODEL": "vendor/model",
                "LLM_MODEL_FAST": "claude-haiku-5-5",
            },
            "LLM_MODEL_FAST",
        ),
        (
            {
                "LLM_PROVIDER": "anthropic",
                "ANTHROPIC_API_KEY": "sk-ant-not-real",
                "LLM_FALLBACK_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "sk-or-not-real",
                "LLM_FALLBACK_MODEL": "vendor/model",
                "LLM_FALLBACK_MODEL_FAST": "claude-haiku-5-5",
            },
            "LLM_FALLBACK_MODEL_FAST",
        ),
    ],
)
def test_a_fast_model_of_the_wrong_shape_is_refused_by_its_key(
    values: dict[str, str], key: str
) -> None:
    """The example's Anthropic fast model left in place after switching to
    OpenRouter would fail every fast call; startup names the key instead."""
    with pytest.raises(ValidationError, match=key):
        LLMConfig(_env_file=None, **values)


def test_a_fast_model_of_the_right_shape_is_accepted() -> None:
    """The shape check must not refuse the pair .env.example ships."""
    config = LLMConfig(
        _env_file=None,
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="sk-ant-not-real",
        LLM_MODEL_FAST="claude-haiku-5-5",
        LLM_FALLBACK_PROVIDER="openrouter",
        OPENROUTER_API_KEY="sk-or-not-real",
        LLM_FALLBACK_MODEL="vendor/model",
        LLM_FALLBACK_MODEL_FAST="vendor/model-lite",
    )
    assert config.LLM_MODEL_FAST == "claude-haiku-5-5"
    assert config.LLM_FALLBACK_MODEL_FAST == "vendor/model-lite"
