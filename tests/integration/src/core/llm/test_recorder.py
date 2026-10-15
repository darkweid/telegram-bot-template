from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.core.llm import recording as recording_module
from src.core.llm.enums import LLMCallStatus, LLMProviderName
from src.core.llm.models import LLMUsageRecord
from src.core.llm.recording import LLMUsageRecorder
from src.core.llm.repository import LLMUsageRepository
from src.core.llm.types import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    TextBlock,
)
from src.core.update_context import UpdateContext, bind_update_context
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
    request = LLMRequest(
        purpose=purpose, messages=[LLMMessage(role="user", content="hi")]
    )
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
            request,
            response,
            None,
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
