from datetime import UTC, datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import Chat, Message
import pytest

from src.core.telegram.messages import safe_edit_text
from tests.fakes.telegram import FakeTelegramSession, make_bot


def _message(session: FakeTelegramSession) -> Message:
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        text="old",
    )
    return message.as_(make_bot(session))


async def test_edits_the_message() -> None:
    session = FakeTelegramSession()
    assert await safe_edit_text(_message(session), "new") is not None
    assert session.sent(EditMessageText)[0].text == "new"


async def test_unchanged_text_is_not_an_error() -> None:
    session = FakeTelegramSession()
    session.queue(
        EditMessageText,
        TelegramBadRequest(
            EditMessageText(text="old"),
            "Bad Request: message is not modified: specified new message content "
            "and reply markup are exactly the same as a current content and "
            "reply markup of the message",
        ),
    )
    assert await safe_edit_text(_message(session), "old") is None


async def test_other_bad_requests_still_raise() -> None:
    session = FakeTelegramSession()
    session.queue(
        EditMessageText,
        TelegramBadRequest(
            EditMessageText(text="x"), "Bad Request: message to edit not found"
        ),
    )
    with pytest.raises(TelegramBadRequest):
        await safe_edit_text(_message(session), "x")
