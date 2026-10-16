from decimal import Decimal
import json

from aiogram.utils.i18n import I18n
from pydantic import SecretStr
import pytest

from src.core.llm.disabled import DisabledLLMClient
from src.core.llm.enums import LLMProviderName, ModelTier
from src.core.llm.errors import LLMNotConfigured, LLMUnavailable
from src.core.llm.fallback import FallbackLLMClient
from src.core.llm.interface import LLMClient
from src.core.llm.providers.anthropic import AnthropicLLMClient
from src.core.llm.providers.openrouter import OpenRouterLLMClient
from src.core.llm.speech.disabled import DisabledTranscriber
from src.core.llm.speech.interface import Transcriber
from src.core.llm.speech.openrouter import OpenRouterTranscriber
from src.core.llm.speech.recording import RecordingTranscriber
from src.core.llm.types import LLMMessage, LLMRequest, LLMUsage
from src.core.llm.usage.pricing import PricingTable
from src.core.llm.usage.recording import LLMUsageRecorder, RecordingLLMClient
from src.core.llm.usage.repository import LLMUsageRepository
from src.main.config import Config
from src.main.di import _tier_models, build_container
from tests.fakes.llm import FakeLLMClient
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.llm import record_closing, with_llm
from tests.helpers.logs import captured_logs
from tests.helpers.providers import SessionOnly, TestOverrides, build_test_container

REQUEST = LLMRequest(
    purpose="note_summary", messages=[LLMMessage(role="user", content="hi")]
)
ANTHROPIC = {
    "LLM_PROVIDER": "anthropic",
    "LLM_MODEL": "claude-sonnet-5-5",
    "ANTHROPIC_API_KEY": "sk-ant-not-real",
}
WITH_FALLBACK = {
    **ANTHROPIC,
    "LLM_FALLBACK_PROVIDER": "openrouter",
    "LLM_FALLBACK_MODEL": "vendor/fallback-model",
    "OPENROUTER_API_KEY": "sk-or-not-real",
}


async def test_an_empty_provider_disables_the_llm_without_any_key(
    settings: Config, i18n: I18n
) -> None:
    """A bot that never calls a model must start with no key at all."""
    config = with_llm(settings, LLM_PROVIDER=None)
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        client = await container.get(LLMClient)
        assert isinstance(client, DisabledLLMClient)
        with pytest.raises(LLMNotConfigured):
            await client.complete(REQUEST)
    finally:
        await container.close()


async def test_one_provider_is_recorded_and_closed_with_the_container(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK client holds a connection pool bound to this event loop; left
    open, it outlives the loop and warns at exit."""
    closed: list[str] = []
    record_closing(monkeypatch, closed)
    config = with_llm(settings, **ANTHROPIC)
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        client = await container.get(LLMClient)
        assert isinstance(client, RecordingLLMClient)
        assert isinstance(client.inner, AnthropicLLMClient)
        async with container() as request_scope:
            assert await request_scope.get(LLMClient) is client
        await container.get(LLMUsageRepository)
        assert closed == []
    finally:
        await container.close()
    assert closed == ["AnthropicLLMClient"]


async def test_a_fallback_provider_records_each_provider_apart(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each attempt is a call the provider may bill: a failed primary and the
    fallback that answered are two rows, under their own providers."""
    closed: list[str] = []
    record_closing(monkeypatch, closed)
    config = with_llm(settings, **WITH_FALLBACK)
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        client = await container.get(LLMClient)
        assert isinstance(client, FallbackLLMClient)
        assert isinstance(client.primary, RecordingLLMClient)
        assert isinstance(client.primary.inner, AnthropicLLMClient)
        assert isinstance(client.fallback, RecordingLLMClient)
        assert isinstance(client.fallback.inner, OpenRouterLLMClient)
    finally:
        await container.close()
    assert sorted(closed) == ["AnthropicLLMClient", "OpenRouterLLMClient"]


async def test_each_provider_gets_a_model_per_tier(
    settings: Config, i18n: I18n
) -> None:
    """An empty fast model means the strong one; a fallback keeps the tier
    with its own models, since a model in the request is the primary's id."""
    config = with_llm(settings, **WITH_FALLBACK, LLM_MODEL_FAST="claude-haiku-5-5")
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        client = await container.get(LLMClient)
        assert isinstance(client, FallbackLLMClient)
        assert isinstance(client.primary, RecordingLLMClient)
        assert isinstance(client.fallback, RecordingLLMClient)
        assert dict(client.primary.models) == {
            ModelTier.STRONG: "claude-sonnet-5-5",
            ModelTier.FAST: "claude-haiku-5-5",
        }
        assert dict(client.fallback.models) == {
            ModelTier.STRONG: "vendor/fallback-model",
            ModelTier.FAST: "vendor/fallback-model",
        }
    finally:
        await container.close()


async def test_one_price_table_with_the_overrides_serves_the_adapter(
    settings: Config, i18n: I18n
) -> None:
    """LLM_PRICING_OVERRIDES must reach the cost of every call, and every
    consumer must read the same table."""
    overrides = json.dumps(
        {
            "claude-sonnet-5-5": {
                "input_per_mtok": "1",
                "output_per_mtok": "2",
                "cache_write_per_mtok": "3",
                "cache_read_per_mtok": "4",
            }
        }
    )
    config = with_llm(settings, **ANTHROPIC, LLM_PRICING_OVERRIDES=overrides)
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        table = await container.get(PricingTable)
        client = await container.get(LLMClient)
        assert isinstance(client, RecordingLLMClient)
        assert isinstance(client.inner, AnthropicLLMClient)
        assert client.inner._pricing is table
        assert table.cost(
            "claude-sonnet-5-5", LLMUsage(input_tokens=1_000_000, output_tokens=0)
        ) == Decimal("1")
    finally:
        await container.close()


async def test_a_missing_key_fails_by_its_name(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config refuses this combination; a Config copied around its validator
    must still fail at startup by the key's name, and must not leave the
    primary's client open."""
    closed: list[str] = []
    record_closing(monkeypatch, closed)
    valid = with_llm(settings, **WITH_FALLBACK)
    # model_copy skips the validator, as a Config assembled by hand would.
    config = valid.model_copy(
        update={
            "llm": valid.llm.model_copy(update={"OPENROUTER_API_KEY": SecretStr("")})
        }
    )
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            await container.get(LLMClient)
    finally:
        await container.close()
    assert closed == ["AnthropicLLMClient"]


async def test_handler_tests_get_the_fake_client(settings: Config, i18n: I18n) -> None:
    container = build_test_container(
        with_llm(settings, **ANTHROPIC), FakeTelegramSession(), i18n
    )
    try:
        assert isinstance(await container.get(LLMClient), FakeLLMClient)
    finally:
        await container.close()


def test_an_empty_fast_model_falls_back_to_the_strong_one() -> None:
    """A bot that sets one model must get it for every tier."""
    assert dict(_tier_models("a", "")) == {ModelTier.STRONG: "a", ModelTier.FAST: "a"}


def test_a_fast_model_takes_the_fast_tier_only() -> None:
    assert dict(_tier_models("a", "b")) == {ModelTier.STRONG: "a", ModelTier.FAST: "b"}


def test_the_tier_mapping_is_read_only() -> None:
    """Both adapters and the recorder share one mapping; a write would change
    the model of every later call."""
    models = _tier_models("a", "b")
    with pytest.raises(TypeError):
        models[ModelTier.FAST] = "c"  # type: ignore[index]


TRANSCRIBER = {
    "OPENROUTER_API_KEY": "sk-or-not-real",
    "TRANSCRIBE_MODEL": "google/gemini-3.8-flash",
}


async def test_an_empty_transcribe_model_gives_the_disabled_transcriber(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bot without voice input must start with no OpenRouter key."""
    closed: list[str] = []
    record_closing(monkeypatch, closed)
    config = with_llm(settings, LLM_PROVIDER=None)
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        assert isinstance(await container.get(Transcriber), DisabledTranscriber)
    finally:
        await container.close()
    assert closed == []


async def test_a_transcriber_is_recorded_and_closed_with_the_container(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK client holds a connection pool bound to this event loop; left
    open, it outlives the loop and warns at exit."""
    closed: list[str] = []
    record_closing(monkeypatch, closed)
    config = with_llm(settings, LLM_PROVIDER=None, **TRANSCRIBER)
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        transcriber = await container.get(Transcriber)
        assert isinstance(transcriber, RecordingTranscriber)
        assert isinstance(transcriber.inner, OpenRouterTranscriber)
        assert closed == []
    finally:
        await container.close()
    assert closed == ["OpenRouterTranscriber"]


async def test_the_transcriber_does_not_need_the_llm_provider(
    settings: Config, i18n: I18n
) -> None:
    """Voice input may run beside an LLM layer that is off."""
    config = with_llm(settings, LLM_PROVIDER=None, **TRANSCRIBER)
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        assert isinstance(await container.get(LLMClient), DisabledLLMClient)
        assert isinstance(await container.get(Transcriber), RecordingTranscriber)
    finally:
        await container.close()


async def test_handler_tests_get_the_fake_transcriber(
    settings: Config, i18n: I18n
) -> None:
    """Unit tests must never reach a provider: they queue answers on the fake."""
    overrides = TestOverrides(FakeTelegramSession(), i18n)
    container = build_container(settings, overrides)
    try:
        assert await container.get(Transcriber) is overrides.transcriber
    finally:
        await container.close()


async def test_the_usage_row_names_the_transcription_model(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The llm_usage row must name what was called, not the chat model beside it."""
    recorded: list[dict[str, object]] = []

    async def failing(self: object, *args: object, **kwargs: object) -> None:
        raise LLMUnavailable("down")

    async def capture(self: object, **kwargs: object) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(OpenRouterTranscriber, "transcribe", failing)
    monkeypatch.setattr(LLMUsageRecorder, "record", capture)
    config = with_llm(settings, **{**WITH_FALLBACK, **TRANSCRIBER})
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        transcriber = await container.get(Transcriber)
        with pytest.raises(LLMUnavailable):
            await transcriber.transcribe(
                b"\x00" * 10, "audio/ogg", language=None, purpose="voice_note"
            )
    finally:
        await container.close()
    assert len(recorded) == 1
    assert recorded[0]["provider"] == LLMProviderName.OPENROUTER
    assert recorded[0]["model"] == TRANSCRIBER["TRANSCRIBE_MODEL"]


async def test_the_transcriber_works_beside_an_anthropic_client(
    settings: Config, i18n: I18n
) -> None:
    """Voice input goes through OpenRouter whatever the chat provider is."""
    config = with_llm(settings, **ANTHROPIC, **TRANSCRIBER)
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        client = await container.get(LLMClient)
        assert isinstance(client, RecordingLLMClient)
        assert isinstance(client.inner, AnthropicLLMClient)
        transcriber = await container.get(Transcriber)
        assert isinstance(transcriber, RecordingTranscriber)
        assert isinstance(transcriber.inner, OpenRouterTranscriber)
    finally:
        await container.close()


async def test_a_transcriber_without_its_key_fails_by_the_keys_name(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config refuses this combination; a Config copied around its validator
    must still fail at startup by the key's name, and open no client."""
    closed: list[str] = []
    record_closing(monkeypatch, closed)
    valid = with_llm(settings, LLM_PROVIDER=None, **TRANSCRIBER)
    # model_copy skips the validator, as a Config assembled by hand would.
    config = valid.model_copy(
        update={
            "llm": valid.llm.model_copy(update={"OPENROUTER_API_KEY": SecretStr("")})
        }
    )
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            await container.get(Transcriber)
    finally:
        await container.close()
    assert closed == []


async def _unavailable(self: object, *args: object, **kwargs: object) -> None:
    raise LLMUnavailable("down")


async def _discard(self: object, **kwargs: object) -> None:
    pass


@pytest.mark.parametrize("log_content", [True, False], ids=["on", "off"])
async def test_llm_log_content_reaches_the_transcriber(
    settings: Config,
    i18n: I18n,
    monkeypatch: pytest.MonkeyPatch,
    log_content: bool,
) -> None:
    """A lost flag silently drops the debugging line; a crossed one writes what
    users said into the log where nobody asked for it."""
    monkeypatch.setattr(OpenRouterTranscriber, "transcribe", _unavailable)
    monkeypatch.setattr(LLMUsageRecorder, "record", _discard)
    debug = settings.model_copy(
        update={"app": settings.app.model_copy(update={"DEBUG": True})}
    )
    config = with_llm(
        debug, LLM_PROVIDER=None, LLM_LOG_CONTENT=log_content, **TRANSCRIBER
    )
    container = build_container(config, SessionOnly(FakeTelegramSession(), i18n))
    try:
        transcriber = await container.get(Transcriber)
        with captured_logs("src.core.llm.speech.recording") as records:
            with pytest.raises(LLMUnavailable):
                await transcriber.transcribe(
                    b"\x00" * 10, "audio/ogg", language=None, purpose="voice_note"
                )
    finally:
        await container.close()
    lines = [r.getMessage() for r in records if "Transcription for" in r.getMessage()]
    assert len(lines) == (1 if log_content else 0)
