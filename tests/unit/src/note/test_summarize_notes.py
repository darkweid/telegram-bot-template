from unittest.mock import AsyncMock

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.llm.errors import LLMOutputError, LLMTimeout
from src.note.exceptions import NoteSummaryFailedError
from src.note.models import Note
from src.note.prompts import NOTE_SUMMARY_PURPOSE, NoteSummary
from src.note.repositories import NoteRepository
from src.note.usecases.summarize_notes import (
    SUMMARY_MAX_TOKENS,
    SUMMARY_NOTE_LIMIT,
    NoteSummaryResult,
    SummarizeNotesUseCase,
)
from tests.fakes.db import FakeAsyncSession
from tests.fakes.llm import FakeLLMClient

USER_ID = 42
SUMMARY = NoteSummary(summary="Groceries and calls.", themes=["shopping"])


def _use_case(
    texts: list[str], llm: FakeLLMClient
) -> tuple[SummarizeNotesUseCase, FakeAsyncSession, AsyncMock]:
    session = FakeAsyncSession()
    notes = AsyncMock(spec=NoteRepository)
    notes.latest_for_user.return_value = [
        Note(user_id=USER_ID, text=text) for text in texts
    ]
    return (
        SummarizeNotesUseCase(SQLAlchemyUnitOfWork(session), notes, llm),
        session,
        notes,
    )


async def test_the_latest_notes_are_summarized_in_the_users_language() -> None:
    """The prompt is English; the reply language is the user's, passed in."""
    llm = FakeLLMClient()
    llm.queue_structured(SUMMARY)
    use_case, session, notes = _use_case(["buy milk", "call Ann"], llm)

    result = await use_case.execute(USER_ID, "ru")

    assert result == NoteSummaryResult(note_count=2, summary=SUMMARY)
    notes.latest_for_user.assert_awaited_once_with(session, USER_ID, SUMMARY_NOTE_LIMIT)
    [request] = llm.calls
    assert request.purpose == NOTE_SUMMARY_PURPOSE
    assert request.system is not None and "Write in Russian" in request.system
    assert request.max_tokens == SUMMARY_MAX_TOKENS
    assert request.temperature is None
    [message] = request.messages
    assert message.role == "user"
    assert "<note>buy milk</note>" in message.content
    assert "<note>call Ann</note>" in message.content


async def test_no_notes_means_no_paid_call() -> None:
    """An empty prompt costs money and returns nonsense."""
    llm = FakeLLMClient()
    use_case, _, _ = _use_case([], llm)

    assert await use_case.execute(USER_ID, "en") is None
    assert llm.calls == []


async def test_the_model_is_called_after_the_transaction_ends() -> None:
    """A call that takes seconds must not hold a pooled connection idle in a
    transaction; the read commits nothing."""
    llm = FakeLLMClient()
    llm.queue_structured(SUMMARY)
    use_case, session, _ = _use_case(["buy milk"], llm)
    in_transaction_at_call: list[bool] = []
    original = llm.structured

    async def spy(*args: object, **kwargs: object) -> object:
        in_transaction_at_call.append(session.in_transaction())
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    llm.structured = spy  # type: ignore[method-assign]
    await use_case.execute(USER_ID, "en")

    assert in_transaction_at_call == [False]
    session.commit.assert_not_awaited()


async def test_an_llm_error_reaches_the_caller() -> None:
    """The error handler turns each LLM error into its own reply; swallowing
    it here would leave the user without an answer."""
    llm = FakeLLMClient()
    llm.queue(LLMTimeout())
    use_case, _, _ = _use_case(["buy milk"], llm)

    with pytest.raises(LLMTimeout):
        await use_case.execute(USER_ID, "en")


async def test_an_unusable_answer_becomes_a_failed_summary() -> None:
    """An answer cut at max_tokens is not repaired: the user must hear that
    the summary failed, and Sentry must see the model error behind it."""
    llm = FakeLLMClient()
    llm.queue(LLMOutputError())
    use_case, _, _ = _use_case(["buy milk"], llm)

    with pytest.raises(NoteSummaryFailedError) as caught:
        await use_case.execute(USER_ID, "en")

    assert isinstance(caught.value.__cause__, LLMOutputError)
