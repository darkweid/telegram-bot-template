import asyncio
from dataclasses import replace
import logging
from unittest.mock import AsyncMock

from pydantic import BaseModel
import pytest

from src.core.llm import recording as recording_module
from src.core.llm.enums import LLMProviderName, ModelTier
from src.core.llm.errors import LLMOutputError, LLMTimeout, LLMUnavailable
from src.core.llm.recording import LLMUsageRecorder, RecordingLLMClient
from src.core.llm.types import (
    ImageBlock,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ProviderBlock,
    StructuredResponse,
    ToolResultBlock,
)
from tests.fakes.llm import FakeLLMClient, llm_response, tool_call
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


async def test_log_content_names_images_and_provider_blocks_without_their_bytes() -> (
    None
):
    """The log line is for reading the exchange; image bytes would flood it,
    and a provider block is opaque anyway."""
    inner = FakeLLMClient()
    inner.queue_tool_calls(tool_call("lookup", {"query": "rice"}, id="call_9"))
    client, _ = _client(inner, log_content=True)
    request = replace(
        REQUEST,
        messages=[
            LLMMessage(
                role="user",
                content=[
                    ImageBlock(media_type="image/png", data=b"\x89PNG-secret-bytes")
                ],
            ),
            LLMMessage(
                role="assistant",
                content=[
                    ProviderBlock(
                        provider=LLMProviderName.ANTHROPIC,
                        data={"thinking": "private"},
                    )
                ],
            ),
            LLMMessage(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id="call_8", content="found", is_error=True
                    )
                ],
            ),
        ],
    )
    with captured_logs("src.core.llm.recording") as records:
        await client.complete(request)
    message = records[0].getMessage()
    assert "<image image/png 17 bytes>" in message
    assert "PNG-secret" not in message
    assert "<provider anthropic" in message
    assert "private" not in message
    assert "<tool_result call_8 error found>" in message
    assert '<tool_use lookup {"query": "rice"}>' in message


class HangingLLMClient(FakeLLMClient):
    """Takes the call, then waits until it is cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def structured[T: BaseModel](
        self, request: LLMRequest, output: type[T]
    ) -> StructuredResponse[T]:
        self.calls.append(request)
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def _cancel_call(
    client: RecordingLLMClient, inner: HangingLLMClient, request: LLMRequest
) -> None:
    task = asyncio.create_task(client.complete(request))
    async with asyncio.timeout(2):
        await inner.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_cancelled_call_is_recorded_without_usage() -> None:
    """A provider may bill a call that was cut short; /stats must show it as
    a failed call of the model the tier asked for."""
    inner = HangingLLMClient()
    client, recorder = _client(inner)
    request = replace(REQUEST, tier=ModelTier.FAST)
    await _cancel_call(client, inner, request)
    recorder.record.assert_awaited_once()
    args, kwargs = recorder.record.await_args
    assert args == (request, None, None)
    assert kwargs["cancelled"] is True
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["provider"] == LLMProviderName.ANTHROPIC


async def test_a_cancelled_structured_call_is_recorded_too() -> None:
    """The deadline cuts structured calls the same way."""
    inner = HangingLLMClient()
    client, recorder = _client(inner)
    task = asyncio.create_task(client.structured(REQUEST, Answer))
    async with asyncio.timeout(2):
        await inner.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert recorder.record.await_args.kwargs["cancelled"] is True


async def test_a_cancelled_call_writes_one_log_line() -> None:
    """Every call leaves one line, a cancelled one included; the adapter
    was cut at its await and writes none."""
    inner = HangingLLMClient()
    client, _ = _client(inner)
    with captured_logs("src.core.llm.reporting") as records:
        await _cancel_call(client, inner, REQUEST)
    [record] = records
    assert record.levelno == logging.WARNING
    assert "outcome=Cancelled" in record.getMessage()


async def test_a_deadline_cancel_still_records_and_reads_as_a_timeout() -> None:
    """The agent's deadline cancels the call through asyncio.timeout: the row
    is written and the caller still sees its own TimeoutError."""
    inner = HangingLLMClient()
    client, recorder = _client(inner)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await client.complete(REQUEST)
    assert recorder.record.await_args.kwargs["cancelled"] is True


async def test_a_failed_record_of_a_cancelled_call_does_not_replace_the_cancel() -> (
    None
):
    """A stopping bot must stop: a broken write cannot turn the cancel into
    an error the caller would handle."""
    inner = HangingLLMClient()
    client, recorder = _client(inner)
    recorder.record.side_effect = RuntimeError("database is gone")
    with captured_logs("src.core.llm.recording") as records:
        await _cancel_call(client, inner, REQUEST)
    assert [r.getMessage() for r in records] == [
        "Cancelled LLM call not recorded: RuntimeError"
    ]


async def test_a_hung_record_of_a_cancelled_call_gives_up_after_its_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write runs inside the shutdown budget; a hung database must not
    eat it."""
    monkeypatch.setattr(recording_module, "CANCELLED_RECORD_SECONDS", 0.05)
    inner = HangingLLMClient()
    client, recorder = _client(inner)

    async def hang(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    recorder.record.side_effect = hang
    with captured_logs("src.core.llm.recording") as records:
        await _cancel_call(client, inner, REQUEST)
    assert [r.getMessage() for r in records] == [
        "Cancelled LLM call not recorded: TimeoutError"
    ]


def _hang_first_record(recorder: AsyncMock) -> asyncio.Event:
    entered = asyncio.Event()

    async def record(*args: object, **kwargs: object) -> None:
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()

    recorder.record.side_effect = record
    return entered


async def test_a_cancel_during_the_success_record_still_records_the_answer() -> None:
    """The provider answered and billed: a deadline or a shutdown landing
    while the row is written must not leave the paid call unaccounted."""
    inner = FakeLLMClient()
    response = llm_response("hi", model="claude-sonnet-5-5", latency_ms=640)
    inner.queue(response)
    client, recorder = _client(inner)
    entered = _hang_first_record(recorder)
    task = asyncio.create_task(client.complete(REQUEST))
    async with asyncio.timeout(2):
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert recorder.record.await_count == 2
    args, kwargs = recorder.record.await_args
    assert args == (REQUEST, response, None)
    assert kwargs["model"] == "claude-sonnet-5-5"


async def test_a_cancel_during_the_failure_record_still_records_the_error() -> None:
    """A failed call may be billed too; the cancel goes on, the row stays."""
    inner = FakeLLMClient()
    error = LLMUnavailable("overloaded", provider=LLMProviderName.ANTHROPIC)
    inner.queue(error)
    client, recorder = _client(inner)
    entered = _hang_first_record(recorder)
    task = asyncio.create_task(client.complete(REQUEST))
    async with asyncio.timeout(2):
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert recorder.record.await_count == 2
    args, _ = recorder.record.await_args
    assert args == (REQUEST, None, error)


async def test_a_hung_record_cut_by_a_cancel_gives_up_after_its_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second write runs inside the shutdown budget like a cancelled
    call's own row."""
    monkeypatch.setattr(recording_module, "CANCELLED_RECORD_SECONDS", 0.05)
    inner = FakeLLMClient()
    inner.queue(llm_response("hi"))
    client, recorder = _client(inner)
    entered = asyncio.Event()

    async def hang(*args: object, **kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    recorder.record.side_effect = hang
    task = asyncio.create_task(client.complete(REQUEST))
    with captured_logs("src.core.llm.recording") as records:
        async with asyncio.timeout(2):
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert [r.getMessage() for r in records] == [
        "LLM call cut while recorded, not recorded: TimeoutError"
    ]
