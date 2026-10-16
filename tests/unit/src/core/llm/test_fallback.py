import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.methods import SendMessage, TelegramMethod
from pydantic import BaseModel
import pytest

from src.core.limiter.limiter import RateLimiter
from src.core.llm.enums import LLMProviderName, ModelTier
from src.core.llm.errors import (
    LLMError,
    LLMOutputError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
    LLMUnavailable,
)
from src.core.llm.fallback import FallbackLLMClient
from src.core.llm.providers.anthropic import AnthropicLLMClient
from src.core.llm.providers.openrouter import OpenRouterLLMClient
from src.core.llm.types import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ProviderBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from src.core.llm.usage.pricing import PricingTable
from src.core.llm.usage.recording import LLMUsageRecorder, RecordingLLMClient
from src.core.redis.degradation import RedisDegradationReporter
from src.core.telegram.alerts import DeveloperAlerter
from src.core.update_context import UpdateContext, bind_update_context
from tests.fakes.llm import FakeLLMClient, llm_response, tool_call
from tests.fakes.redis import InMemoryRedis
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.helpers.llm_bodies import anthropic_thinking, openrouter_completion
from tests.helpers.llm_server import ScriptedLLMServer
from tests.helpers.logs import captured_logs

REQUEST = LLMRequest(
    purpose="note_summary",
    messages=[LLMMessage(role="user", content="hi")],
    model="claude-haiku-5-5",
)
FALLBACK_MODEL = "vendor/fallback-model"


class Answer(BaseModel):
    summary: str


def _fallback_answer(text: str = "from the fallback") -> LLMResponse:
    return llm_response(text, provider=LLMProviderName.OPENROUTER, model=FALLBACK_MODEL)


def _alerter(session: FakeTelegramSession | None = None) -> DeveloperAlerter:
    return DeveloperAlerter(
        make_bot(session or FakeTelegramSession()),
        RateLimiter(
            InMemoryRedis(),
            prefix="alerts",
            reporter=RedisDegradationReporter("Alerts"),
        ),
        recipients=(-100500,),
        cooldown_seconds=300,
    )


def _client(
    session: FakeTelegramSession | None = None,
    alerter: DeveloperAlerter | None = None,
) -> tuple[FallbackLLMClient, FakeLLMClient, FakeLLMClient]:
    primary, fallback = FakeLLMClient(), FakeLLMClient()
    client = FallbackLLMClient(primary, fallback, alerter or _alerter(session))
    return client, primary, fallback


class _StalledTelegramSession(FakeTelegramSession):
    """Holds every message until `release` is set: an alert stuck in flood
    control or on a slow network."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - signature fixed by BaseSession
    ) -> Any:
        await self.release.wait()
        return await super().make_request(bot, method, timeout)


@pytest.mark.parametrize("error_type", [LLMUnavailable, LLMTimeout, LLMRateLimited])
async def test_a_provider_outage_is_answered_by_the_fallback(
    error_type: type[LLMError],
) -> None:
    """One vendor's outage must not take the bot's answers with it."""
    client, primary, fallback = _client()
    primary.queue(error_type("down", provider=LLMProviderName.ANTHROPIC))
    answer = _fallback_answer()
    fallback.queue(answer)
    assert await client.complete(REQUEST) is answer


async def test_a_healthy_primary_answers_alone() -> None:
    client, primary, fallback = _client()
    answer = llm_response("from the primary")
    primary.queue(answer)
    assert await client.complete(REQUEST) is answer
    assert fallback.calls == []


async def test_the_fallback_answers_with_its_own_model() -> None:
    """An Anthropic model id means nothing to the other vendor; the fallback
    uses the model it was configured with."""
    client, primary, fallback = _client()
    primary.queue(LLMUnavailable("down", provider=LLMProviderName.ANTHROPIC))
    await client.complete(REQUEST)
    assert primary.calls[0].model == "claude-haiku-5-5"
    [sent] = fallback.calls
    assert sent.model is None
    assert (sent.purpose, sent.messages) == (REQUEST.purpose, REQUEST.messages)


@pytest.mark.parametrize("error_type", [LLMRefused, LLMOutputError])
async def test_a_failure_of_the_request_is_not_sent_elsewhere(
    error_type: type[LLMError],
) -> None:
    """A refused prompt or an unparsable answer would most likely fail with
    the other vendor too, and cost twice."""
    session = FakeTelegramSession()
    client, primary, fallback = _client(session)
    primary.queue(error_type("no", provider=LLMProviderName.ANTHROPIC))
    with pytest.raises(error_type):
        await client.complete(REQUEST)
    assert fallback.calls == []
    assert session.sent(SendMessage) == []


async def test_developers_hear_of_a_switch_once_per_cooldown() -> None:
    """During an outage every call switches: one alert per cooldown is news,
    one per call is noise."""
    session = FakeTelegramSession()
    alerter = _alerter(session)
    client, primary, fallback = _client(alerter=alerter)
    context = UpdateContext(update_id=1, update_type="message", user_id=42, chat_id=42)
    with bind_update_context(context):
        for _ in range(3):
            primary.queue(LLMUnavailable("down", provider=LLMProviderName.ANTHROPIC))
            await client.complete(REQUEST)
    await alerter.drain()
    assert len(fallback.calls) == 3
    [alert] = session.sent(SendMessage)
    assert "LLMUnavailable" in alert.text
    assert "llm anthropic" in alert.text
    assert "user 42" in alert.text


async def test_a_slow_alert_does_not_hold_up_the_fallbacks_answer() -> None:
    """The user is already waiting out the primary's timeout and retries; an
    alert stuck in flood control must not add to it."""
    session = _StalledTelegramSession()
    alerter = _alerter(session)
    client, primary, fallback = _client(alerter=alerter)
    primary.queue(LLMUnavailable("down", provider=LLMProviderName.ANTHROPIC))
    answer = _fallback_answer()
    fallback.queue(answer)

    async with asyncio.timeout(1):
        assert await client.complete(REQUEST) is answer

    assert session.sent(SendMessage) == []
    session.release.set()
    await alerter.drain()
    [alert] = session.sent(SendMessage)
    assert "LLMUnavailable" in alert.text


async def test_when_both_fail_the_fallbacks_error_is_raised() -> None:
    """The caller answers the user from the error that ended the call."""
    client, primary, fallback = _client()
    primary.queue(LLMUnavailable("down", provider=LLMProviderName.ANTHROPIC))
    error = LLMTimeout("slow", provider=LLMProviderName.OPENROUTER)
    fallback.queue(error)
    with pytest.raises(LLMTimeout) as raised:
        await client.complete(REQUEST)
    assert raised.value is error


async def test_a_structured_call_falls_back_too() -> None:
    client, primary, fallback = _client()
    primary.queue(LLMRateLimited("429", provider=LLMProviderName.ANTHROPIC))
    fallback.queue_structured(Answer(summary="short"))
    result = await client.structured(REQUEST, Answer)
    assert result.output == Answer(summary="short")
    assert fallback.calls[0].model is None


async def test_the_switch_is_logged_without_content() -> None:
    client, primary, _ = _client()
    primary.queue(LLMUnavailable("down", provider=LLMProviderName.ANTHROPIC))
    with captured_logs("src.core.llm.fallback") as records:
        await client.complete(REQUEST)
    [record] = records
    assert record.getMessage() == (
        "LLM anthropic failed for note_summary with LLMUnavailable, "
        "using the fallback"
    )


async def test_a_failed_primary_and_the_answering_fallback_are_two_rows() -> None:
    """Each attempt is a call the provider may bill: the usage rows must name
    the provider that failed and the one that answered."""
    primary, fallback = FakeLLMClient(), FakeLLMClient()
    recorder = AsyncMock(spec=LLMUsageRecorder)
    error = LLMUnavailable("down", provider=LLMProviderName.ANTHROPIC)
    primary.queue(error)
    answer = _fallback_answer()
    fallback.queue(answer)
    client = FallbackLLMClient(
        RecordingLLMClient(
            primary,
            recorder,
            provider=LLMProviderName.ANTHROPIC,
            models={
                ModelTier.STRONG: "claude-sonnet-5-5",
                ModelTier.FAST: "claude-sonnet-5-5",
            },
        ),
        RecordingLLMClient(
            fallback,
            recorder,
            provider=LLMProviderName.OPENROUTER,
            models={
                ModelTier.STRONG: FALLBACK_MODEL,
                ModelTier.FAST: FALLBACK_MODEL,
            },
        ),
        _alerter(),
    )

    assert await client.complete(REQUEST) is answer

    first, second = recorder.record.await_args_list
    assert (first.kwargs["purpose"], first.kwargs["error"]) == (REQUEST.purpose, error)
    assert first.kwargs["usage"] is None
    assert first.kwargs["provider"] is LLMProviderName.ANTHROPIC
    assert second.kwargs["usage"] is answer.usage
    assert second.kwargs["error"] is None
    assert second.kwargs["provider"] is LLMProviderName.OPENROUTER
    assert second.kwargs["model"] == FALLBACK_MODEL


async def test_the_fallback_gets_the_tool_history_at_the_same_tier() -> None:
    """The adapters translate the neutral history themselves, so a switch in
    the middle of a tool loop sends the same turns; the tier stays, the
    primary's model id goes."""
    client, primary, fallback = _client()
    primary.queue(LLMUnavailable("down", provider=LLMProviderName.ANTHROPIC))
    fallback.queue(_fallback_answer())
    call = tool_call("lookup", {"query": "rice"})
    history = (
        LLMMessage(role="user", content="rice"),
        LLMMessage(
            role="assistant",
            content=(
                ProviderBlock(
                    provider=LLMProviderName.ANTHROPIC, data={"type": "thinking"}
                ),
                call,
            ),
        ),
        LLMMessage(
            role="user",
            content=(ToolResultBlock(tool_use_id=call.id, content="found"),),
        ),
    )

    await client.complete(replace(REQUEST, messages=history, tier=ModelTier.FAST))

    [sent] = fallback.calls
    assert sent.messages == history
    assert sent.tier is ModelTier.FAST
    assert sent.model is None


async def test_a_switch_mid_tool_loop_sends_the_history_without_the_primarys_blocks() -> (
    None
):
    """The fake test above only shows the same history reaches the fallback;
    this one shows the real adapter turns it into a body OpenRouter accepts,
    with the signed Anthropic thinking it cannot read left out."""
    anthropic_server, openrouter_server = ScriptedLLMServer(), ScriptedLLMServer()
    anthropic_server.reply(
        {"type": "error", "error": {"type": "api_error", "message": "down"}},
        status=500,
    )
    openrouter_server.reply(openrouter_completion("Rice: found."))
    primary = AnthropicLLMClient(
        api_key="sk-ant-not-real",
        models={ModelTier.STRONG: "claude-sonnet-5-5"},
        pricing=PricingTable(),
        timeout_seconds=7.0,
        max_retries=0,
        http_client=anthropic_server.http_client(),
    )
    fallback = OpenRouterLLMClient(
        api_key="sk-or-not-real",
        models={ModelTier.STRONG: FALLBACK_MODEL},
        app_url="",
        app_title="",
        timeout_seconds=7.0,
        max_retries=0,
        http_client=openrouter_server.http_client(),
    )
    client = FallbackLLMClient(primary, fallback, _alerter())
    call = ToolUseBlock(id="toolu_1", name="lookup", input={"query": "rice"})
    history = (
        LLMMessage(role="user", content="rice"),
        LLMMessage(
            role="assistant",
            content=(
                ProviderBlock(
                    provider=LLMProviderName.ANTHROPIC, data=anthropic_thinking()
                ),
                call,
            ),
        ),
        LLMMessage(
            role="user",
            content=(ToolResultBlock(tool_use_id="toolu_1", content="found"),),
        ),
    )

    response = await client.complete(replace(REQUEST, messages=history, model=None))

    assert response.text == "Rice: found."
    assert len(anthropic_server.requests) == 1
    [body] = openrouter_server.bodies()
    assert body["model"] == FALLBACK_MODEL
    assert body["messages"] == [
        {"role": "user", "content": "rice"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"query": "rice"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "found"},
    ]
