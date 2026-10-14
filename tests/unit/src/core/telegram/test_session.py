from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
import pytest

from src.core.telegram.session import (
    MAX_RETRY_AFTER_SECONDS,
    ForbiddenMiddleware,
    RetryAfterMiddleware,
)
from tests.fakes.telegram import FakeTelegramSession, make_bot


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class Recorder:
    def __init__(self) -> None:
        self.unreachable: list[int] = []

    async def record_unreachable(self, user_id: int) -> None:
        self.unreachable.append(user_id)


def _flood(seconds: int, chat_id: int = 42) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        SendMessage(chat_id=chat_id, text="x"), "Too Many Requests", seconds
    )


def _forbidden(chat_id: int) -> TelegramForbiddenError:
    return TelegramForbiddenError(
        SendMessage(chat_id=chat_id, text="x"), "bot was blocked by the user"
    )


async def test_flood_wait_is_waited_out_and_retried() -> None:
    session, sleeps = FakeTelegramSession(), Sleeps()
    session.middleware(RetryAfterMiddleware(attempts=3, sleep=sleeps))
    session.queue(SendMessage, _flood(2))
    session.queue(SendMessage, _flood(5))
    await make_bot(session).send_message(chat_id=42, text="x")
    assert sleeps.calls == [2, 5]
    assert len(session.sent(SendMessage)) == 3


async def test_retries_are_bounded() -> None:
    session, sleeps = FakeTelegramSession(), Sleeps()
    session.middleware(RetryAfterMiddleware(attempts=2, sleep=sleeps))
    for _ in range(3):
        session.queue(SendMessage, _flood(1))
    with pytest.raises(TelegramRetryAfter):
        await make_bot(session).send_message(chat_id=42, text="x")
    assert sleeps.calls == [1, 1]


async def test_a_wait_longer_than_the_cap_is_not_waited() -> None:
    session, sleeps = FakeTelegramSession(), Sleeps()
    session.middleware(RetryAfterMiddleware(attempts=3, sleep=sleeps))
    session.queue(SendMessage, _flood(MAX_RETRY_AFTER_SECONDS + 1))
    with pytest.raises(TelegramRetryAfter):
        await make_bot(session).send_message(chat_id=42, text="x")
    assert sleeps.calls == []


async def test_other_errors_pass_through_untouched() -> None:
    session, sleeps = FakeTelegramSession(), Sleeps()
    session.middleware(RetryAfterMiddleware(attempts=3, sleep=sleeps))
    session.queue(
        SendMessage,
        TelegramBadRequest(SendMessage(chat_id=42, text="x"), "chat not found"),
    )
    with pytest.raises(TelegramBadRequest):
        await make_bot(session).send_message(chat_id=42, text="x")
    assert sleeps.calls == []


async def test_forbidden_in_a_private_chat_records_the_user_as_unreachable_and_reraises() -> (
    None
):
    session, recorder = FakeTelegramSession(), Recorder()
    session.middleware(ForbiddenMiddleware(recorder))
    session.queue(SendMessage, _forbidden(42))
    with pytest.raises(TelegramForbiddenError):
        await make_bot(session).send_message(chat_id=42, text="x")
    assert recorder.unreachable == [42]


@pytest.mark.parametrize(
    "reason",
    ["Forbidden: user is deactivated", "Forbidden: bot was blocked by the user"],
)
async def test_any_forbidden_in_a_private_chat_is_recorded(reason: str) -> None:
    session, recorder = FakeTelegramSession(), Recorder()
    session.middleware(ForbiddenMiddleware(recorder))
    session.queue(
        SendMessage,
        TelegramForbiddenError(SendMessage(chat_id=42, text="x"), reason),
    )
    with pytest.raises(TelegramForbiddenError):
        await make_bot(session).send_message(chat_id=42, text="x")
    assert recorder.unreachable == [42]


async def test_no_retries_with_zero_attempts() -> None:
    session, sleeps = FakeTelegramSession(), Sleeps()
    session.middleware(RetryAfterMiddleware(attempts=0, sleep=sleeps))
    session.queue(SendMessage, _flood(1))
    with pytest.raises(TelegramRetryAfter):
        await make_bot(session).send_message(chat_id=42, text="x")
    assert sleeps.calls == []


async def test_a_wait_exactly_at_the_cap_is_waited() -> None:
    session, sleeps = FakeTelegramSession(), Sleeps()
    session.middleware(RetryAfterMiddleware(attempts=1, sleep=sleeps))
    session.queue(SendMessage, _flood(MAX_RETRY_AFTER_SECONDS))
    await make_bot(session).send_message(chat_id=42, text="x")
    assert sleeps.calls == [MAX_RETRY_AFTER_SECONDS]


async def test_a_retried_call_ending_in_forbidden_is_recorded_once() -> None:
    session, sleeps, recorder = FakeTelegramSession(), Sleeps(), Recorder()
    session.middleware(RetryAfterMiddleware(attempts=3, sleep=sleeps))
    session.middleware(ForbiddenMiddleware(recorder))
    session.queue(SendMessage, _flood(2))
    session.queue(SendMessage, _forbidden(42))
    with pytest.raises(TelegramForbiddenError):
        await make_bot(session).send_message(chat_id=42, text="x")
    assert recorder.unreachable == [42]
    assert sleeps.calls == [2]


@pytest.mark.parametrize("chat_id", [-100123, "@channel"])
async def test_forbidden_in_a_group_or_channel_does_not_make_a_user_unreachable(
    chat_id: int | str,
) -> None:
    session, recorder = FakeTelegramSession(), Recorder()
    session.middleware(ForbiddenMiddleware(recorder))
    session.queue(
        SendMessage,
        TelegramForbiddenError(
            SendMessage(chat_id=chat_id, text="x"), "bot was kicked"
        ),
    )
    with pytest.raises(TelegramForbiddenError):
        await make_bot(session).send_message(chat_id=chat_id, text="x")
    assert recorder.unreachable == []


async def test_a_failing_recorder_does_not_hide_the_telegram_error() -> None:
    class Broken:
        async def record_unreachable(self, user_id: int) -> None:
            raise ConnectionError("database down")

    session = FakeTelegramSession()
    session.middleware(ForbiddenMiddleware(Broken()))
    session.queue(SendMessage, _forbidden(42))
    with pytest.raises(TelegramForbiddenError):
        await make_bot(session).send_message(chat_id=42, text="x")
