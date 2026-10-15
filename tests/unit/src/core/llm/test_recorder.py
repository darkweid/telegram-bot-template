from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sentry_sdk
from sqlalchemy.exc import DBAPIError

from src.core.errors.scrubbed import ScrubbedError
from src.core.llm import recording as recording_module
from src.core.llm.enums import LLMCallStatus, LLMProviderName
from src.core.llm.errors import LLMOutputError, LLMTimeout
from src.core.llm.models import LLMUsageRecord
from src.core.llm.recording import LLMUsageRecorder
from src.core.llm.repository import LLMUsageRepository
from src.core.llm.types import LLMMessage, LLMRequest, LLMResponse, LLMUsage
from src.core.update_context import UpdateContext, bind_update_context
from tests.fakes.db import FakeSessionFactory
from tests.helpers.logs import captured_logs

NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recording_module, "get_utc_now", lambda: NOW)


def _request(model: str | None = None) -> LLMRequest:
    return LLMRequest(
        purpose="note_summary",
        messages=[LLMMessage(role="user", content="secret note text")],
        model=model,
    )


def _response() -> LLMResponse:
    return LLMResponse(
        text="secret answer",
        usage=LLMUsage(
            input_tokens=120,
            output_tokens=40,
            cache_read_tokens=100,
            cache_write_tokens=20,
            cost_usd=Decimal("0.00123400"),
        ),
        model="claude-sonnet-5-5",
        provider=LLMProviderName.ANTHROPIC,
        stop_reason="end_turn",
        latency_ms=812,
    )


def _stored(sessions: FakeSessionFactory) -> LLMUsageRecord:
    [(row,), _] = sessions.session.add.call_args
    assert isinstance(row, LLMUsageRecord)
    return row


async def test_a_successful_call_is_stored_with_its_usage_and_cost() -> None:
    sessions = FakeSessionFactory()
    recorder = LLMUsageRecorder(sessions, LLMUsageRepository())
    with bind_update_context(
        UpdateContext(update_id=1, update_type="message", user_id=42, chat_id=42)
    ):
        await recorder.record(
            _request(),
            _response(),
            None,
            provider=LLMProviderName.ANTHROPIC,
            model="claude-sonnet-5-5",
            latency_ms=812,
        )
    row = _stored(sessions)
    assert (row.created_at, row.user_id, row.purpose) == (NOW, 42, "note_summary")
    assert (row.provider, row.model) == ("anthropic", "claude-sonnet-5-5")
    assert (
        row.input_tokens,
        row.output_tokens,
        row.cache_read_tokens,
        row.cache_write_tokens,
    ) == (120, 40, 100, 20)
    assert row.cost_usd == Decimal("0.00123400")
    assert (row.latency_ms, row.status, row.error_type) == (
        812,
        LLMCallStatus.SUCCESS,
        None,
    )
    sessions.session.commit.assert_awaited_once()


async def test_a_failed_call_is_stored_by_its_error_class() -> None:
    """A provider that times out costs nothing in tokens but still has to show
    up in /stats: an outage is otherwise invisible in the accounting."""
    sessions = FakeSessionFactory()
    recorder = LLMUsageRecorder(sessions, LLMUsageRepository())
    await recorder.record(
        _request(),
        None,
        LLMTimeout("read timeout", provider=LLMProviderName.ANTHROPIC),
        provider=LLMProviderName.ANTHROPIC,
        model="claude-sonnet-5-5",
        latency_ms=30000,
    )
    row = _stored(sessions)
    assert (row.status, row.error_type) == (LLMCallStatus.ERROR, "LLMTimeout")
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (0, 0, None)
    assert row.latency_ms == 30000


async def test_a_failed_repair_is_stored_with_what_both_answers_cost() -> None:
    """Two answers that failed validation were billed; /stats must show the
    money spent on them, not a free error."""
    sessions = FakeSessionFactory()
    paid = LLMUsage(input_tokens=240, output_tokens=80, cost_usd=Decimal("0.00246800"))
    await LLMUsageRecorder(sessions, LLMUsageRepository()).record(
        _request(),
        None,
        LLMOutputError(
            "points: too_short", provider=LLMProviderName.ANTHROPIC, usage=paid
        ),
        provider=LLMProviderName.ANTHROPIC,
        model="claude-sonnet-5-5",
        latency_ms=1600,
    )
    row = _stored(sessions)
    assert (row.status, row.error_type) == (LLMCallStatus.ERROR, "LLMOutputError")
    assert (row.input_tokens, row.output_tokens) == (240, 80)
    assert row.cost_usd == Decimal("0.00246800")


async def test_a_call_outside_an_update_has_no_user() -> None:
    """Worker tasks call the model too; they have no update context."""
    sessions = FakeSessionFactory()
    await LLMUsageRecorder(sessions, LLMUsageRepository()).record(
        _request(),
        _response(),
        None,
        provider=LLMProviderName.ANTHROPIC,
        model="claude-sonnet-5-5",
        latency_ms=812,
    )
    assert _stored(sessions).user_id is None


async def test_a_failed_write_never_fails_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user already paid for the answer with their wait; losing one
    accounting row is cheaper than losing the answer. The driver's DETAIL can
    echo the row, so only the scrubbed error reaches the log and Sentry."""
    captured: list[BaseException] = []
    monkeypatch.setattr(sentry_sdk, "capture_exception", captured.append)
    sessions = FakeSessionFactory()
    sessions.session.commit.side_effect = DBAPIError(
        "INSERT INTO llm_usage ...",
        {"purpose": "note_summary"},
        Exception("Failing row contains (secret)"),
    )
    with captured_logs("src.core.llm.recording") as records:
        await LLMUsageRecorder(sessions, LLMUsageRepository()).record(
            _request(),
            _response(),
            None,
            provider=LLMProviderName.ANTHROPIC,
            model="claude-sonnet-5-5",
            latency_ms=812,
        )
    [record] = records
    assert record.getMessage() == "LLM usage not recorded: DBAPIError"
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], ScrubbedError)
    [event] = captured
    assert isinstance(event, ScrubbedError)
    assert "secret" not in str(event)


async def test_no_prompt_or_answer_text_reaches_the_row() -> None:
    """llm_usage is read by /stats and kept for months: it holds counts only."""
    sessions = FakeSessionFactory()
    await LLMUsageRecorder(sessions, LLMUsageRepository()).record(
        _request(),
        _response(),
        None,
        provider=LLMProviderName.ANTHROPIC,
        model="claude-sonnet-5-5",
        latency_ms=812,
    )
    row = _stored(sessions)
    values = [getattr(row, column.key) for column in LLMUsageRecord.__table__.columns]
    assert not any(isinstance(value, str) and "secret" in value for value in values)
