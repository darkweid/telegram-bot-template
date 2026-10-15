from collections.abc import Sequence
import traceback
from typing import cast
from unittest.mock import AsyncMock

from aiogram.methods import SendChatAction, SendMessage
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.llm.errors import (
    LLMError,
    LLMNotConfigured,
    LLMOutputError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
    LLMUnavailable,
)
from src.core.llm.interface import LLMClient
from src.main.config import Config, ThrottlingConfig
from src.note.exceptions import NoteSummaryFailedError
from src.note.models import Note
from src.note.prompts import NoteSummary
from src.note.repositories import NoteRepository
from tests.factories.updates import build_message_update
from tests.fakes.db import FakeAsyncSession
from tests.fakes.llm import FakeLLMClient
from tests.helpers.dispatcher import RunningBot, running_bot
from tests.helpers.logs import captured_logs
from tests.helpers.user import UseCases

USER_ID = 42
# Answered by the error handler with the error's own text; LLMOutputError is
# turned into NoteSummaryFailedError by the use case.
LLM_ERRORS_SHOWN_AS_IS: tuple[type[LLMError], ...] = (
    LLMNotConfigured,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    LLMRefused,
)
ALL_LLM_ERRORS = (*LLM_ERRORS_SHOWN_AS_IS, LLMOutputError)

type Answer = NoteSummary | BaseException


class NoteStub(Provider):
    """Stored notes in memory; the use case is real, and the model is the
    FakeLLMClient that TestOverrides already binds."""

    def __init__(self, *texts: str) -> None:
        super().__init__()
        self.notes = AsyncMock(spec=NoteRepository)
        self.notes.latest_for_user.return_value = [
            Note(user_id=USER_ID, text=text) for text in texts
        ]

    @provide(scope=Scope.APP, override=True)
    def note_repository(self) -> NoteRepository:
        return self.notes

    @provide(scope=Scope.REQUEST, override=True)
    def session(self) -> AsyncSession:
        return cast(AsyncSession, FakeAsyncSession())


async def _fake_llm(running: RunningBot, answers: Sequence[Answer]) -> FakeLLMClient:
    llm = await running.container.get(LLMClient)
    assert isinstance(llm, FakeLLMClient)
    for answer in answers:
        if isinstance(answer, NoteSummary):
            llm.queue_structured(answer)
        else:
            llm.queue(answer)
    return llm


async def _summarize(
    settings: Config,
    i18n: I18n,
    stub: NoteStub,
    *answers: Answer,
    count: int = 1,
    locale: str | None = None,
    language_code: str = "en",
) -> tuple[list[str], FakeLLMClient]:
    async with running_bot(settings, i18n, UseCases(locale), stub) as running:
        llm = await _fake_llm(running, answers)
        for _ in range(count):
            await running.feed(
                build_message_update(
                    "/summarize", user_id=USER_ID, language_code=language_code
                )
            )
        replies = [reply.text or "" for reply in running.session.sent(SendMessage)]
    return replies, llm


async def test_the_summary_is_sent_escaped_under_a_translated_header(
    settings: Config, i18n: I18n
) -> None:
    """The model's text comes from what the user typed: unescaped, a "<" in a
    note would break HTML parsing of the whole reply."""
    [reply], _ = await _summarize(
        settings,
        i18n,
        NoteStub("buy milk", "call <Ann>"),
        NoteSummary(summary="Milk and a call to <Ann>.", themes=["errands"]),
    )
    assert reply == (
        "Summary of 2 recent notes:\n\n"
        "Milk and a call to &lt;Ann&gt;.\n\n"
        "• errands"
    )


async def test_the_stored_language_choice_picks_the_summary_language(
    settings: Config, i18n: I18n
) -> None:
    """A user who chose Russian in /language reads Russian, whatever language
    their Telegram app uses."""
    [reply], llm = await _summarize(
        settings,
        i18n,
        NoteStub("buy milk"),
        NoteSummary(summary="Молоко.", themes=[]),
        locale="ru",
        language_code="en",
    )
    [request] = llm.calls
    assert request.system is not None and "Write in Russian" in request.system
    assert reply == "Сводка по 1 последней заметке:\n\nМолоко."


async def test_the_typing_indicator_comes_before_the_summary(
    settings: Config, i18n: I18n
) -> None:
    """The model takes seconds; without it the command looks ignored."""
    async with running_bot(settings, i18n, UseCases(), NoteStub("buy milk")) as running:
        await _fake_llm(running, [NoteSummary(summary="Milk.", themes=[])])
        await running.feed(build_message_update("/summarize", user_id=USER_ID))
        kinds = [type(request) for request in running.session.requests]
    assert kinds.index(SendChatAction) < kinds.index(SendMessage)


async def test_without_notes_the_user_is_told_and_nothing_is_paid(
    settings: Config, i18n: I18n
) -> None:
    """An empty prompt costs money; the user needs to know why there is no
    summary rather than see an error."""
    [reply], llm = await _summarize(settings, i18n, NoteStub())
    assert reply == "You have no notes yet. Send /note to write one."
    assert llm.calls == []


@pytest.mark.parametrize("error_class", LLM_ERRORS_SHOWN_AS_IS)
async def test_each_llm_error_gets_its_own_translated_reply(
    settings: Config, i18n: I18n, error_class: type[LLMError]
) -> None:
    """ "LLM is off", "busy" and "declined" ask different things of the user:
    wait, try later, or rephrase. One generic text would hide which."""
    [reply], _ = await _summarize(
        settings, i18n, NoteStub("buy milk"), error_class(), language_code="ru"
    )
    assert reply == i18n.gettext(error_class.message_key, locale="ru")
    assert reply != error_class.message_key


async def test_an_unusable_answer_is_reported_as_a_failed_summary(
    settings: Config, i18n: I18n
) -> None:
    """An answer cut at max_tokens or invalid after the repair reaches the
    user as a failed summary, not as a model that "could not be read"."""
    [reply], _ = await _summarize(
        settings, i18n, NoteStub("buy milk"), LLMOutputError(), language_code="ru"
    )
    assert reply == i18n.gettext(NoteSummaryFailedError.message_key, locale="ru")
    assert reply != NoteSummaryFailedError.message_key


def test_every_reply_to_a_failure_is_distinct() -> None:
    """Two failures sharing a text would make the replies above
    indistinguishable to the user."""
    keys = [error_class.message_key for error_class in ALL_LLM_ERRORS]
    keys.append(NoteSummaryFailedError.message_key)
    assert len(set(keys)) == len(keys)


async def test_note_text_stays_out_of_the_logs(settings: Config, i18n: I18n) -> None:
    """Notes are what users typed: CLAUDE.md keeps them out of logs and
    Sentry, and a failed call is where a careless detail would leak them."""
    secret = "my card PIN is 4931"
    with captured_logs("src.core.telegram.errors") as records:
        _, llm = await _summarize(settings, i18n, NoteStub(secret), LLMOutputError())
    assert records
    assert llm.calls and secret in llm.calls[0].messages[0].content
    for record in records:
        logged = record.getMessage()
        if record.exc_info is not None:
            logged += "".join(traceback.format_exception(*record.exc_info))
        assert secret not in logged


async def test_summarize_is_limited_by_the_llm_profile(
    settings: Config, i18n: I18n
) -> None:
    """Every call costs money: the LLM profile is far tighter than the
    default one."""
    tight = settings.model_copy(
        update={
            "throttling": ThrottlingConfig(
                THROTTLE_LLM_LIMIT=1, THROTTLE_LLM_WINDOW_SECONDS=60
            )
        }
    )
    replies, llm = await _summarize(
        tight,
        i18n,
        NoteStub("buy milk"),
        NoteSummary(summary="Milk.", themes=[]),
        count=2,
    )
    assert len(llm.calls) == 1
    assert replies[1].startswith("Too many requests")
