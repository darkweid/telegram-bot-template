from aiogram.utils.i18n import I18n
from pydantic import SecretStr
import pytest

from src.core.llm.anthropic import AnthropicLLMClient
from src.core.llm.disabled import DisabledLLMClient
from src.core.llm.errors import LLMNotConfigured
from src.core.llm.fallback import FallbackLLMClient
from src.core.llm.interface import LLMClient
from src.core.llm.openrouter import OpenRouterLLMClient
from src.core.llm.recording import RecordingLLMClient
from src.core.llm.repository import LLMUsageRepository
from src.core.llm.types import LLMMessage, LLMRequest
from src.main.config import Config
from src.main.di import build_container
from tests.fakes.llm import FakeLLMClient
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.llm import record_closing, with_llm
from tests.helpers.providers import SessionOnly, build_test_container

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
