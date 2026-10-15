from dataclasses import replace
from unittest.mock import AsyncMock

from pydantic import BaseModel
import pytest

from src.core.llm.enums import LLMProviderName, ModelTier
from src.core.llm.errors import LLMOutputError, LLMTimeout, LLMUnavailable
from src.core.llm.recording import LLMUsageRecorder, RecordingLLMClient
from src.core.llm.types import LLMMessage, LLMRequest
from tests.fakes.llm import FakeLLMClient, llm_response
from tests.helpers.logs import captured_logs

REQUEST = LLMRequest(
    purpose="note_summary",
    messages=[LLMMessage(role="user", content="secret note")],
    system="be brief",
)


class Answer(BaseModel):
    summary: str


def _client(
    inner: FakeLLMClient, *, log_content: bool = False
) -> tuple[RecordingLLMClient, AsyncMock]:
    recorder = AsyncMock(spec=LLMUsageRecorder)
    client = RecordingLLMClient(
        inner,
        recorder,
        provider=LLMProviderName.ANTHROPIC,
        models={
            ModelTier.STRONG: "claude-sonnet-5-5",
            ModelTier.FAST: "claude-haiku-5-5",
        },
        log_content=log_content,
    )
    return client, recorder


async def test_a_success_is_recorded_as_the_provider_reported_it() -> None:
    """The response names the model that answered and was billed, and its
    latency is the provider call's, not the recorder's."""
    inner = FakeLLMClient()
    response = llm_response("hi", model="claude-sonnet-5-5", latency_ms=640)
    inner.queue(response)
    client, recorder = _client(inner)
    assert await client.complete(REQUEST) is response
    recorder.record.assert_awaited_once_with(
        REQUEST,
        response,
        None,
        provider=LLMProviderName.ANTHROPIC,
        model="claude-sonnet-5-5",
        latency_ms=640,
    )


async def test_a_failure_is_recorded_and_still_raised() -> None:
    """An outage must show in /stats, and the caller still has to answer the
    user from the error."""
    inner = FakeLLMClient()
    error = LLMUnavailable("overloaded", provider=LLMProviderName.ANTHROPIC)
    inner.queue(error)
    client, recorder = _client(inner)
    with pytest.raises(LLMUnavailable) as raised:
        await client.complete(REQUEST)
    assert raised.value is error
    args, kwargs = recorder.record.await_args
    assert args == (REQUEST, None, error)
    assert kwargs["provider"] == LLMProviderName.ANTHROPIC
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert isinstance(kwargs["latency_ms"], int)
    assert kwargs["latency_ms"] >= 0


async def test_a_failed_call_names_the_model_it_asked_for() -> None:
    """A purpose that asks for a cheaper model is accounted under it even
    when the call fails."""
    inner = FakeLLMClient()
    inner.queue(LLMTimeout("read timeout", provider=LLMProviderName.ANTHROPIC))
    client, recorder = _client(inner)
    with pytest.raises(LLMTimeout):
        await client.complete(replace(REQUEST, model="claude-haiku-5-5"))
    assert recorder.record.await_args.kwargs["model"] == "claude-haiku-5-5"


async def test_a_structured_answer_is_recorded_by_its_response() -> None:
    inner = FakeLLMClient()
    inner.queue_structured(Answer(summary="short"))
    client, recorder = _client(inner)
    result = await client.structured(REQUEST, Answer)
    assert result.output == Answer(summary="short")
    recorder.record.assert_awaited_once_with(
        REQUEST,
        result.response,
        None,
        provider=result.response.provider,
        model=result.response.model,
        latency_ms=result.response.latency_ms,
    )


async def test_a_structured_failure_is_recorded() -> None:
    """Two invalid answers were still two billed calls."""
    inner = FakeLLMClient()
    error = LLMOutputError("invalid after repair", provider=LLMProviderName.ANTHROPIC)
    inner.queue(error)
    client, recorder = _client(inner)
    with pytest.raises(LLMOutputError):
        await client.structured(REQUEST, Answer)
    assert recorder.record.await_args.args[2] is error


async def test_content_stays_out_of_the_logs_by_default() -> None:
    """Prompts carry what users wrote; logs are kept and shipped elsewhere."""
    client, _ = _client(FakeLLMClient())
    with captured_logs("src.core.llm.recording") as records:
        await client.complete(REQUEST)
    assert records == []


async def test_log_content_shows_the_exchange_for_local_debugging() -> None:
    inner = FakeLLMClient()
    inner.queue(llm_response("the answer"))
    client, _ = _client(inner, log_content=True)
    with captured_logs("src.core.llm.recording") as records:
        await client.complete(REQUEST)
    [record] = records
    message = record.getMessage()
    assert "be brief" in message
    assert "secret note" in message
    assert "the answer" in message


async def test_a_failed_fast_call_is_recorded_under_the_fast_model() -> None:
    """A failure has no response to name its model; recording it under the
    strong one would put the fast tier's errors on the wrong line of /stats."""
    inner = FakeLLMClient()
    inner.queue(LLMTimeout("slow", provider=LLMProviderName.ANTHROPIC))
    client, recorder = _client(inner)

    with pytest.raises(LLMTimeout):
        await client.complete(replace(REQUEST, tier=ModelTier.FAST))

    assert recorder.record.await_args.kwargs["model"] == "claude-haiku-5-5"
