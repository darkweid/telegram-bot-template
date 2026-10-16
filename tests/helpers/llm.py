from collections.abc import Awaitable, Callable

import pytest

from src.core.llm.providers.anthropic import AnthropicLLMClient
from src.core.llm.providers.openrouter import OpenRouterLLMClient
from src.core.llm.speech.openrouter import OpenRouterTranscriber
from src.main.config import Config, LLMConfig


def with_llm(settings: Config, **values: object) -> Config:
    """`settings` with an LLM section built from `values` alone: no env file,
    so the developer's .env never decides which provider a test gets."""
    return settings.model_copy(update={"llm": LLMConfig(_env_file=None, **values)})


def record_closing(monkeypatch: pytest.MonkeyPatch, closed: list[str]) -> None:
    """Append the adapter's class name to `closed` when it closes; the real
    close still runs, so no SDK connection pool is left behind."""
    for adapter in (AnthropicLLMClient, OpenRouterLLMClient, OpenRouterTranscriber):
        monkeypatch.setattr(adapter, "aclose", _recording(adapter.aclose, closed))


def _recording(
    original: Callable[[object], Awaitable[None]], closed: list[str]
) -> Callable[[object], Awaitable[None]]:
    async def aclose(self: object) -> None:
        closed.append(type(self).__name__)
        await original(self)

    return aclose
