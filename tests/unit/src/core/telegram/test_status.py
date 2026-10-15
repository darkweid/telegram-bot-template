import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import (
    EditMessageText,
    SendChatAction,
    SendMessage,
    TelegramMethod,
)
from aiogram.utils.i18n import I18n
from pydantic import BaseModel, ValidationError
import pytest

from src.core.errors.exceptions import CoreException
from src.core.i18n import N_
from src.core.telegram import status as status_module
from src.core.telegram.session import RetryAfterMiddleware
from src.core.telegram.status import ProcessingStatus
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.helpers.logs import captured_logs

CHAT_ID = 42
GENERIC = "Something went wrong. Please try again later."


class NoteGone(CoreException):
    message_key = N_("Note {name} is gone.")


class Count(BaseModel):
    value: int


class FakeClock:
    """The ticker's clock, stepped by the test; `looked` returns once the
    ticker went once more round its loop, with no wall-clock wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.reads = 0
        self._read = asyncio.Event()

    def __call__(self) -> float:
        self.reads += 1
        self._read.set()
        return self.now

    async def looked(self) -> None:
        # Two reads: the top of the loop, then the wait at its end.
        target = self.reads + 2
        async with asyncio.timeout(2):
            while self.reads < target:
                self._read.clear()
                await self._read.wait()


class HoldingSession(FakeTelegramSession):
    """Holds every editMessageText until `release` is set; `held` is set
    once one is waiting."""

    def __init__(self) -> None:
        super().__init__()
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - signature fixed by BaseSession
    ) -> Any:
        if isinstance(method, EditMessageText):
            self.held.set()
            await self.release.wait()
        return await super().make_request(bot, method, timeout)


@pytest.fixture(autouse=True)
def fast_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_module, "TYPING_INTERVAL_SECONDS", 0.2)
    monkeypatch.setattr(status_module, "EDIT_INTERVAL_SECONDS", 0.1)


def _bot(session: FakeTelegramSession, sleeps: list[float] | None = None) -> Bot:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    session.middleware(RetryAfterMiddleware(3, sleep=sleep))
    return make_bot(session)


def _flood(method: TelegramMethod[Any], seconds: int = 3) -> TelegramRetryAfter:
    return TelegramRetryAfter(method, "Too Many Requests", seconds)


def _edits(session: FakeTelegramSession) -> list[str]:
    return [edit.text for edit in session.sent(EditMessageText)]


async def _send(
    session: FakeTelegramSession, i18n: I18n, locale: str = "en", **kwargs: Any
) -> ProcessingStatus:
    with i18n.context(), i18n.use_locale(locale):
        return await ProcessingStatus.send(_bot(session), CHAT_ID, i18n=i18n, **kwargs)


async def test_the_status_is_sent_in_the_users_language(i18n: I18n) -> None:
    """The work runs outside the update; what it shows at the end must still
    be in the language the status was sent in."""
    session = FakeTelegramSession()
    status = await _send(session, i18n, "ru")
    with pytest.raises(ValueError):
        async with status:
            raise ValueError
    [sent] = session.sent(SendMessage)
    assert sent.text == "⏳ Работаю…"
    assert _edits(session) == [
        "Что-то пошло не так. Попробуйте ещё раз чуть позже.",
    ]


async def test_typing_repeats_until_finish(i18n: I18n) -> None:
    """Telegram clears the indicator after five seconds; a long run must
    keep it on."""
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    async with status:
        await session.wait_for(SendChatAction, count=3)
        await status.finish("Done.")
    count = len(session.sent(SendChatAction))
    await asyncio.sleep(0.3)
    assert len(session.sent(SendChatAction)) == count
    assert all(a.action == "typing" for a in session.sent(SendChatAction))


async def test_edits_go_at_most_once_a_second_and_the_latest_wins(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram floods a chat edited faster; a text overtaken before it was
    shown is of no use to the user."""
    clock = FakeClock()
    monkeypatch.setattr(status_module, "_now", clock)
    monkeypatch.setattr(status_module, "EDIT_INTERVAL_SECONDS", 1)
    monkeypatch.setattr(status_module, "TYPING_INTERVAL_SECONDS", 60)
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    async with status:
        status.update("one")
        await session.wait_for(EditMessageText)
        status.update("two")
        status.update("three")
        clock.now = 0.999
        await clock.looked()
        assert _edits(session) == ["⏳ one"]  # the interval has not passed
        clock.now = 1
        await session.wait_for(EditMessageText, count=2)
        await status.finish("Done.")
    assert _edits(session) == ["⏳ one", "⏳ three", "Done."]


async def test_update_never_waits_for_telegram(i18n: I18n) -> None:
    """The work goes on while an edit hangs; the next text replaces the one
    stuck behind it."""
    session = HoldingSession()
    status = await _send(session, i18n)
    async with status:
        status.update("one")
        async with asyncio.timeout(2):
            await session.held.wait()
        # Plain calls: update cannot await the edit stuck in Telegram.
        status.update("two")
        status.update("three")
        session.release.set()
        await session.wait_for(EditMessageText, count=2)
        await status.finish("Done.")
    assert _edits(session) == ["⏳ one", "⏳ three", "Done."]


async def test_an_intermediate_call_is_not_retried_on_flood_control(
    i18n: I18n,
) -> None:
    """Sleeping out a flood wait for a progress edit only holds the ticker:
    the next edit makes it obsolete anyway."""
    session = FakeTelegramSession()
    sleeps: list[float] = []
    with i18n.context():
        status = await ProcessingStatus.send(_bot(session, sleeps), CHAT_ID, i18n=i18n)
    session.queue(
        SendChatAction, _flood(SendChatAction(chat_id=CHAT_ID, action="typing"), 1)
    )
    with captured_logs("src.core.telegram.status") as records:
        async with status:
            await session.wait_for(SendChatAction)
            status.update("one")
            await asyncio.sleep(0.05)
            assert _edits(session) == []  # quiet through Telegram's wait
            await session.wait_for(EditMessageText)
            await status.finish("Done.")
    assert sleeps == []
    assert [r.getMessage() for r in records if r.levelno == logging.WARNING] == [
        "Status typing skipped: flood control for 1s"
    ]
    assert _edits(session) == ["⏳ one", "Done."]


async def test_finish_is_retried_on_flood_control(i18n: I18n) -> None:
    """The answer is what the user waited for: it waits out flood control."""
    session = FakeTelegramSession()
    sleeps: list[float] = []
    with i18n.context():
        status = await ProcessingStatus.send(_bot(session, sleeps), CHAT_ID, i18n=i18n)
    async with status:
        session.queue(
            EditMessageText,
            _flood(EditMessageText(chat_id=CHAT_ID, message_id=1, text="x")),
        )
        await status.finish("Done.")
    assert sleeps == [3]
    assert _edits(session) == ["Done.", "Done."]


async def test_finish_sends_a_new_message_when_the_status_is_gone(
    i18n: I18n,
) -> None:
    """A user who deleted the status still gets the answer."""
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    async with status:
        session.queue(
            EditMessageText,
            TelegramBadRequest(
                EditMessageText(chat_id=CHAT_ID, message_id=1, text="x"),
                "Bad Request: message to edit not found",
            ),
        )
        await status.finish("Done.")
    assert [m.text for m in session.sent(SendMessage)] == ["⏳ Working on it…", "Done."]


async def test_a_finish_telegram_refuses_leaves_the_generic_text(
    i18n: I18n,
) -> None:
    """An answer Telegram refuses (too long, broken HTML) must not leave the
    user watching the status forever."""
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    session.queue(
        EditMessageText,
        TelegramBadRequest(
            EditMessageText(chat_id=CHAT_ID, message_id=1, text="x"),
            "Bad Request: message is too long",
        ),
    )
    with pytest.raises(TelegramBadRequest):
        async with status:
            await status.finish("A very long answer.")
    assert _edits(session) == ["A very long answer.", GENERIC]


async def test_a_cancel_during_the_finish_flood_wait_leaves_interrupted(
    i18n: I18n,
) -> None:
    """A stopping bot cancels a finish that sleeps out flood control; the
    answer never went out, so the user must not keep the status."""
    session = FakeTelegramSession()
    sleeping = asyncio.Event()

    async def sleep(seconds: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    session.middleware(RetryAfterMiddleware(3, sleep=sleep))
    with i18n.context():
        status = await ProcessingStatus.send(make_bot(session), CHAT_ID, i18n=i18n)
    session.queue(
        EditMessageText,
        _flood(EditMessageText(chat_id=CHAT_ID, message_id=1, text="x")),
    )

    async def work() -> None:
        async with status:
            await status.finish("Done.")

    task = asyncio.create_task(work())
    async with asyncio.timeout(2):
        await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _edits(session) == ["Done.", "Interrupted. Please try again."]


async def test_a_core_exception_becomes_its_translated_text_with_params_escaped(
    i18n: I18n,
) -> None:
    """The same text the update's error handler would have sent, and a
    value the user typed cannot break the HTML."""
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    with pytest.raises(NoteGone):
        async with status:
            raise NoteGone(name="<b>x</b>")
    assert _edits(session) == ["Note &lt;b&gt;x&lt;/b&gt; is gone."]


async def test_any_other_error_becomes_the_generic_text(i18n: I18n) -> None:
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    with pytest.raises(KeyError):
        async with status:
            raise KeyError("secret detail")
    assert _edits(session) == [GENERIC]


async def test_a_cancel_edits_the_status_to_interrupted_within_its_bound(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopping bot waits for this edit; a hung Telegram must not hold the
    shutdown past the bound."""
    monkeypatch.setattr(status_module, "INTERRUPTED_EDIT_SECONDS", 0.1)
    session = FakeTelegramSession()
    status = await _send(session, i18n, "ru")
    started = asyncio.Event()

    async def work() -> None:
        async with status:
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _edits(session) == ["Прервано. Попробуйте ещё раз."]

    hung = HoldingSession()
    status = await _send(hung, i18n)
    task = asyncio.create_task(work())
    started.clear()
    await started.wait()
    task.cancel()
    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_leaving_without_finish_is_a_bug(i18n: I18n) -> None:
    """The user is told something went wrong, and the runner sees the bug."""
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    with pytest.raises(RuntimeError, match="without finishing"):
        async with status:
            pass
    assert _edits(session) == [GENERIC]


async def test_a_second_finish_is_refused(i18n: I18n) -> None:
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    async with status:
        await status.finish("Done.")
        with pytest.raises(RuntimeError, match="already finished"):
            await status.finish("Again.")
    assert _edits(session) == ["Done."]


async def test_a_failed_error_edit_keeps_the_works_own_exception(i18n: I18n) -> None:
    """The runner must see what broke the work, not the edit that failed to
    tell the user about it."""
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    session.queue(
        EditMessageText,
        TelegramBadRequest(
            EditMessageText(chat_id=CHAT_ID, message_id=1, text="x"),
            "Bad Request: chat not found",
        ),
    )
    with captured_logs("src.core.telegram.status") as records:
        with pytest.raises(KeyError):
            async with status:
                raise KeyError("x")
    assert [r.getMessage() for r in records] == [
        "Could not tell the user about the error: TelegramBadRequest"
    ]


async def test_a_progress_edit_refused_for_good_is_skipped(i18n: I18n) -> None:
    """Retrying an edit Telegram refuses would log every second of a long
    run; the next text gets its own try."""
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    session.queue(
        EditMessageText,
        TelegramBadRequest(
            EditMessageText(chat_id=CHAT_ID, message_id=1, text="x"),
            "Bad Request: message can't be edited",
        ),
    )
    with captured_logs("src.core.telegram.status") as records:
        async with status:
            status.update("one")
            await session.wait_for(EditMessageText)
            await asyncio.sleep(0.25)
            assert _edits(session) == ["⏳ one"]
            status.update("two")
            await session.wait_for(EditMessageText, count=2)
            await status.finish("Done.")
    assert _edits(session) == ["⏳ one", "⏳ two", "Done."]
    assert [r.getMessage() for r in records] == [
        "Status edit failed: TelegramBadRequest"
    ]


async def test_a_ticker_failure_reaches_the_log_scrubbed(i18n: I18n) -> None:
    """A ticker bug must not carry what it was handed into the log or
    Sentry's breadcrumbs."""
    session = FakeTelegramSession()
    status = await _send(session, i18n)
    typed = {"value": "secret" + "-input"}  # kept off the traceback's source line
    try:
        Count.model_validate(typed)
    except ValidationError as error:
        session.queue(SendChatAction, error)
    with captured_logs("src.core.telegram.status") as records:
        async with status:
            await session.wait_for(SendChatAction)
            await status.finish("Done.")
    [record] = records
    assert record.getMessage() == "Status ticker failed: ValidationError"
    assert record.exc_info is not None
    assert "secret-input" not in logging.Formatter().formatException(record.exc_info)
    assert _edits(session) == ["Done."]
