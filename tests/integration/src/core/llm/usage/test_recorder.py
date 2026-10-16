from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.core.llm.enums import LLMCallStatus, LLMProviderName
from src.core.llm.reporting import billed_usage
from src.core.llm.speech.recording import RecordingTranscriber
from src.core.llm.types import LLMResponse, LLMUsage, TextBlock
from src.core.llm.usage import recording as recording_module
from src.core.llm.usage.models import LLMUsageRecord
from src.core.llm.usage.recording import LLMUsageRecorder
from src.core.llm.usage.repository import LLMUsageRepository
from src.core.update_context import UpdateContext, bind_update_context
from tests.fakes.speech import FakeTranscriber, transcript
from tests.integration.conftest import new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2002, 2, 2, 2, 2, 2, tzinfo=UTC)


async def test_a_recorded_call_is_committed_on_its_own(
    integration_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row must survive whatever the handler's transaction does next, and
    the user id must come from the update being handled."""
    monkeypatch.setattr(recording_module, "get_utc_now", lambda: NOW)
    sessionmaker = async_sessionmaker(integration_engine, expire_on_commit=False)
    purpose = f"test_{uuid4().hex}"
    user_id = new_user_id()
    response = LLMResponse(
        content=(TextBlock("hello"),),
        usage=LLMUsage(input_tokens=7, output_tokens=3, cost_usd=Decimal("0.00004200")),
        model="claude-sonnet-5-5",
        provider=LLMProviderName.ANTHROPIC,
        stop="end",
        stop_reason="end_turn",
        latency_ms=321,
    )
    with bind_update_context(
        UpdateContext(update_id=1, update_type="message", user_id=user_id)
    ):
        await LLMUsageRecorder(sessionmaker, LLMUsageRepository()).record(
            purpose=purpose,
            usage=billed_usage(response.usage, None),
            error=None,
            provider=LLMProviderName.ANTHROPIC,
            model="claude-sonnet-5-5",
            latency_ms=321,
        )
    try:
        async with sessionmaker() as session:
            row = (
                await session.execute(
                    select(LLMUsageRecord).where(LLMUsageRecord.purpose == purpose)
                )
            ).scalar_one()
        assert (row.created_at, row.user_id) == (NOW, user_id)
        assert (row.provider, row.model, row.status) == (
            "anthropic",
            "claude-sonnet-5-5",
            LLMCallStatus.SUCCESS,
        )
        assert (row.input_tokens, row.output_tokens, row.latency_ms) == (7, 3, 321)
        assert row.cost_usd == Decimal("0.00004200")
    finally:
        async with sessionmaker() as session:
            await session.execute(
                delete(LLMUsageRecord).where(LLMUsageRecord.purpose == purpose)
            )
            await session.commit()


CANCELLED_AT = datetime(2003, 3, 3, 3, 3, 3, tzinfo=UTC)


async def test_a_cancelled_call_row_round_trips(
    integration_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/stats counts a cancelled call as an error and never as unpriced: it
    stored no usage, so it used no tokens it could leave out of the sum."""
    monkeypatch.setattr(recording_module, "get_utc_now", lambda: CANCELLED_AT)
    sessionmaker = async_sessionmaker(integration_engine, expire_on_commit=False)
    purpose = f"test_{uuid4().hex}"
    await LLMUsageRecorder(sessionmaker, LLMUsageRepository()).record(
        purpose=purpose,
        usage=None,
        error=None,
        provider=LLMProviderName.ANTHROPIC,
        model="claude-sonnet-5-5",
        latency_ms=1500,
        cancelled=True,
    )
    try:
        async with sessionmaker() as session:
            row = (
                await session.execute(
                    select(LLMUsageRecord).where(LLMUsageRecord.purpose == purpose)
                )
            ).scalar_one()
            summary = await LLMUsageRepository().summary(
                session,
                datetime(2003, 1, 1, tzinfo=UTC),
                datetime(2004, 1, 1, tzinfo=UTC),
            )
        assert (row.status, row.error_type) == (LLMCallStatus.ERROR, "Cancelled")
        assert (row.input_tokens, row.output_tokens, row.cost_usd) == (0, 0, None)
        assert (summary.calls, summary.errors, summary.unpriced_calls) == (1, 1, 0)
        assert summary.cost_usd == Decimal(0)
    finally:
        async with sessionmaker() as session:
            await session.execute(
                delete(LLMUsageRecord).where(LLMUsageRecord.purpose == purpose)
            )
            await session.commit()


TRANSCRIBED_AT = datetime(2004, 4, 4, 4, 4, 4, tzinfo=UTC)


async def test_a_transcription_row_lands_in_llm_usage(
    integration_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Voice input is paid per call like any model call: /stats must see its
    provider, purpose and the cost OpenRouter reported, to the last digit."""
    monkeypatch.setattr(recording_module, "get_utc_now", lambda: TRANSCRIBED_AT)
    sessionmaker = async_sessionmaker(integration_engine, expire_on_commit=False)
    inner = FakeTranscriber()
    inner.queue(transcript(cost=Decimal("0.00012345")))
    transcriber = RecordingTranscriber(
        inner,
        LLMUsageRecorder(sessionmaker, LLMUsageRepository()),
        provider=LLMProviderName.OPENROUTER,
        model="google/gemini-3.8-flash",
    )
    purpose = f"test_{uuid4().hex}"
    in_window = (LLMUsageRecord.purpose == purpose) & (
        LLMUsageRecord.created_at == TRANSCRIBED_AT
    )
    try:
        await transcriber.transcribe(
            b"OggS", "audio/ogg", language=None, purpose=purpose
        )
        async with sessionmaker() as session:
            row = (
                await session.execute(select(LLMUsageRecord).where(in_window))
            ).scalar_one()
        assert (row.provider, row.model, row.status) == (
            "openrouter",
            "google/gemini-3.8-flash",
            LLMCallStatus.SUCCESS,
        )
        assert (row.input_tokens, row.output_tokens) == (20, 6)
        assert row.cost_usd == Decimal("0.00012345")
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(LLMUsageRecord).where(in_window))
            await session.commit()
