import asyncio
import base64
from collections.abc import Callable
from decimal import Decimal
import logging

import pytest

from src.core.llm.enums import LLMCallStatus, LLMProviderName
from src.core.llm.errors import LLMError, LLMRefused
from src.core.llm.speech.disabled import DisabledTranscriber
from src.core.llm.speech.errors import TranscriberNotConfigured
from src.core.llm.speech.interface import Transcriber
from src.core.llm.speech.recording import RecordingTranscriber
from src.core.llm.types import LLMUsage
from src.core.llm.usage.models import LLMUsageRecord
from src.core.llm.usage.recording import LLMUsageRecorder
from src.core.llm.usage.repository import LLMUsageRepository
from tests.fakes.db import FakeSessionFactory
from tests.fakes.speech import FakeTranscriber, transcript
from tests.helpers.logs import captured_logs

AUDIO = b"OggS" + bytes(range(200))
CONFIGURED_MODEL = "google/gemini-3.8-flash-configured"


def _transcriber(
    inner: Transcriber,
    *,
    sessions: FakeSessionFactory | None = None,
    recorder: LLMUsageRecorder | None = None,
    log_content: bool = False,
) -> tuple[RecordingTranscriber, FakeSessionFactory]:
    sessions = sessions or FakeSessionFactory()
    recorder = recorder or LLMUsageRecorder(sessions, LLMUsageRepository())
    wrapped = RecordingTranscriber(
        inner,
        recorder,
        provider=LLMProviderName.OPENROUTER,
        model=CONFIGURED_MODEL,
        log_content=log_content,
    )
    return wrapped, sessions


def _rows(sessions: FakeSessionFactory) -> list[LLMUsageRecord]:
    rows = [call.args[0] for call in sessions.session.add.call_args_list]
    assert all(isinstance(row, LLMUsageRecord) for row in rows)
    return rows


async def _transcribe(
    wrapped: RecordingTranscriber, media_type: str = "audio/ogg"
) -> str:
    result = await wrapped.transcribe(AUDIO, media_type, language=None, purpose="voice")
    return result.text


async def test_a_success_is_one_row_by_the_answering_model() -> None:
    """The provider names the model it routed to and the price it billed;
    /stats must show that, not the configured id."""
    inner = FakeTranscriber()
    answered = transcript(cost=Decimal("0.00042"))
    inner.queue(answered)
    wrapped, sessions = _transcriber(inner)
    assert await _transcribe(wrapped) == answered.text
    [row] = _rows(sessions)
    assert row.purpose == "voice"
    assert row.provider == LLMProviderName.OPENROUTER
    assert row.model == answered.model
    assert row.status == LLMCallStatus.SUCCESS
    assert row.cost_usd == Decimal("0.00042")
    assert (row.input_tokens, row.output_tokens) == (20, 6)
    assert row.latency_ms == answered.latency_ms


async def test_a_billed_failure_is_one_error_row_with_its_usage() -> None:
    """A refused transcription was still paid for, and the caller still has
    to answer the user from the error."""
    inner = FakeTranscriber()
    usage = LLMUsage(input_tokens=310, output_tokens=4, cost_usd=Decimal("0.0002"))
    error = LLMRefused(
        "content_filter", provider=LLMProviderName.OPENROUTER, usage=usage
    )
    inner.queue(error)
    wrapped, sessions = _transcriber(inner)
    with pytest.raises(LLMRefused) as raised:
        await _transcribe(wrapped)
    assert raised.value is error
    [row] = _rows(sessions)
    assert row.status == LLMCallStatus.ERROR
    assert row.error_type == "LLMRefused"
    assert (row.input_tokens, row.output_tokens) == (310, 4)
    assert row.cost_usd == Decimal("0.0002")
    assert row.model == CONFIGURED_MODEL
    assert row.latency_ms >= 0


async def test_a_cancel_mid_call_records_one_cancelled_row() -> None:
    """A deadline or a stopping bot cuts the call: the attempt still shows
    in /stats and in the log, and the cancel reaches the caller as it came."""
    inner = FakeTranscriber()
    hold = inner.hold()
    wrapped, sessions = _transcriber(inner)
    with captured_logs("src.core.llm.reporting") as records:
        task = asyncio.create_task(_transcribe(wrapped))
        async with asyncio.timeout(2):
            await hold.reached.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    [row] = _rows(sessions)
    assert (row.status, row.error_type) == (LLMCallStatus.ERROR, "Cancelled")
    assert row.model == CONFIGURED_MODEL
    assert row.cost_usd is None
    [record] = records
    assert record.levelno == logging.WARNING
    assert "outcome=Cancelled" in record.getMessage()


class SlowRecorder(LLMUsageRecorder):
    """Writes only once `release` is set."""

    def __init__(self, sessions: FakeSessionFactory) -> None:
        super().__init__(sessions, LLMUsageRepository())
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def record(
        self,
        *,
        purpose: str,
        provider: LLMProviderName,
        model: str,
        usage: LLMUsage | None,
        error: LLMError | None,
        latency_ms: int,
        cancelled: bool = False,
    ) -> None:
        self.entered.set()
        await self.release.wait()
        await super().record(
            purpose=purpose,
            provider=provider,
            model=model,
            usage=usage,
            error=error,
            latency_ms=latency_ms,
            cancelled=cancelled,
        )


async def test_a_cancel_during_the_record_keeps_one_row() -> None:
    """The transcription was billed: a cancel landing while its row is
    written lets that write finish and never writes a second row."""
    inner = FakeTranscriber()
    inner.queue(transcript())
    sessions = FakeSessionFactory()
    slow = SlowRecorder(sessions)
    wrapped, _ = _transcriber(inner, sessions=sessions, recorder=slow)
    task = asyncio.create_task(_transcribe(wrapped))
    async with asyncio.timeout(2):
        await slow.entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        slow.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    [row] = _rows(sessions)
    assert row.status == LLMCallStatus.SUCCESS


@pytest.mark.parametrize(
    ("inner", "media_type", "raised"),
    [
        (FakeTranscriber, "text/plain", ValueError),
        (DisabledTranscriber, "audio/ogg", TranscriberNotConfigured),
    ],
    ids=["bad-media-type", "disabled"],
)
async def test_caller_errors_and_a_disabled_transcriber_are_not_recorded(
    inner: Callable[[], Transcriber], media_type: str, raised: type[Exception]
) -> None:
    """A caller bug and a switched-off feature never reach a provider: a row
    would show an outage in /stats that did not happen."""
    wrapped, sessions = _transcriber(inner())
    with pytest.raises(raised):
        await _transcribe(wrapped, media_type)
    assert _rows(sessions) == []


async def test_log_content_shows_the_text_and_only_the_size_of_the_audio() -> None:
    """The line is for reading a transcription while debugging; audio bytes
    or their base64 would flood it and carry the user's voice."""
    inner = FakeTranscriber()
    inner.queue(transcript("two hundred grams of rice"))
    wrapped, _ = _transcriber(inner, log_content=True)
    with captured_logs("src.core.llm.speech.recording") as records:
        await _transcribe(wrapped)
    [record] = records
    message = record.getMessage()
    assert record.levelno == logging.INFO
    assert message.startswith("Transcription for voice: ")
    assert "<audio ogg 204 bytes>" in message
    assert "two hundred grams of rice" in message
    assert base64.b64encode(AUDIO).decode()[:16] not in message
    assert "OggS" not in message


async def test_log_content_names_the_error_class_of_a_failure() -> None:
    """A failed transcription has no text to show; the line must still say
    how the attempt ended, or debugging a refusal reads as silence."""
    inner = FakeTranscriber()
    inner.queue(LLMRefused("content_filter", provider=LLMProviderName.OPENROUTER))
    wrapped, _ = _transcriber(inner, log_content=True)
    with captured_logs("src.core.llm.speech.recording") as records:
        with pytest.raises(LLMRefused):
            await _transcribe(wrapped)
    [record] = records
    assert record.getMessage().endswith("-> LLMRefused")


async def test_the_transcription_stays_out_of_the_logs_by_default() -> None:
    """A transcription is what the user said; logs are kept and shipped
    elsewhere."""
    inner = FakeTranscriber()
    inner.queue(transcript())
    wrapped, _ = _transcriber(inner)
    with captured_logs("src.core.llm.speech.recording") as records:
        await _transcribe(wrapped)
    assert records == []


async def test_log_content_says_a_cancelled_transcription_was_cancelled() -> None:
    """A transcription cut by a deadline or a shutdown leaves no text; the
    line must name the cancel, not look like a call that never ended."""
    inner = FakeTranscriber()
    hold = inner.hold()
    wrapped, _ = _transcriber(inner, log_content=True)
    with captured_logs("src.core.llm.speech.recording") as records:
        task = asyncio.create_task(_transcribe(wrapped))
        async with asyncio.timeout(2):
            await hold.reached.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    [record] = records
    assert record.getMessage() == (
        "Transcription for voice: <audio ogg 204 bytes> -> Cancelled"
    )
