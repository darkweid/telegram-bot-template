from datetime import UTC, datetime

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import EditMessageText, GetChat, SendMessage, SetMyCommands
from aiogram.types import BotCommand, Chat, Message, MessageId
import pytest

from tests.factories.updates import (
    build_callback_update,
    build_message_update,
    build_my_chat_member_update,
)
from tests.fakes.telegram import BOT_ID, FakeTelegramSession, make_bot


async def test_records_requests_and_answers_with_defaults() -> None:
    session = FakeTelegramSession()
    bot = make_bot(session)
    message = await bot.send_message(chat_id=42, text="hello")
    assert isinstance(message, Message)
    assert (message.chat.id, message.text) == (42, "hello")
    assert (
        await bot.set_my_commands([BotCommand(command="start", description="Start")])
        is True
    )
    assert [type(m) for m in session.requests] == [SendMessage, SetMyCommands]
    assert session.sent(SendMessage)[0].text == "hello"


async def test_edit_returns_a_message_for_a_chat_message() -> None:
    session = FakeTelegramSession()
    bot = make_bot(session)
    edited = await bot.edit_message_text(text="new", chat_id=42, message_id=1)
    assert isinstance(edited, Message)
    assert [type(m) for m in session.requests] == [EditMessageText]


async def test_queued_results_and_errors_come_first() -> None:
    session = FakeTelegramSession()
    bot = make_bot(session)
    session.queue(
        SendMessage,
        TelegramForbiddenError(
            SendMessage(chat_id=42, text="x"), "bot was blocked by the user"
        ),
    )
    with pytest.raises(TelegramForbiddenError, match="blocked by the user"):
        await bot.send_message(chat_id=42, text="x")
    assert isinstance(await bot.send_message(chat_id=42, text="y"), Message)


async def test_unqueued_method_without_a_default_is_an_error() -> None:
    bot = make_bot(FakeTelegramSession())
    with pytest.raises(LookupError, match="GetChat"):
        await bot(GetChat(chat_id=42))


async def test_get_me_describes_the_test_bot() -> None:
    me = await make_bot(FakeTelegramSession()).me()
    assert (me.id, me.is_bot) == (BOT_ID, True)


def test_builders_produce_distinct_typed_updates() -> None:
    message = build_message_update("/start", user_id=7, language_code="ru")
    callback = build_callback_update("lang:ru", user_id=7)
    member = build_my_chat_member_update("kicked", user_id=7)
    assert len({message.update_id, callback.update_id, member.update_id}) == 3
    assert message.event_type == "message"
    assert message.message is not None and message.message.chat.id == 7
    assert (
        message.message.from_user is not None
        and message.message.from_user.language_code == "ru"
    )
    assert callback.event_type == "callback_query"
    assert (
        callback.callback_query is not None
        and callback.callback_query.data == "lang:ru"
    )
    assert member.event_type == "my_chat_member"
    assert member.my_chat_member is not None
    assert member.my_chat_member.new_chat_member.status == "kicked"


def test_group_message_has_its_own_chat() -> None:
    update = build_message_update("hi", user_id=7, chat_id=-100, chat_type="supergroup")
    assert update.message is not None
    assert (update.message.chat.id, update.message.chat.type) == (-100, "supergroup")


async def test_queued_results_come_in_fifo_order_and_as_given() -> None:
    session = FakeTelegramSession()
    bot = make_bot(session)
    first = Message(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=1, type="private"),
    )
    second = Message(
        message_id=2,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=1, type="private"),
    )
    session.queue(SendMessage, first)
    session.queue(SendMessage, second)
    assert await bot.send_message(chat_id=42, text="a") is first
    assert await bot.send_message(chat_id=42, text="b") is second


async def test_sent_filters_by_method_type() -> None:
    session = FakeTelegramSession()
    bot = make_bot(session)
    await bot.send_message(chat_id=42, text="a")
    await bot.set_my_commands([BotCommand(command="start", description="Start")])
    assert len(session.sent(SendMessage)) == 1
    assert len(session.sent(SetMyCommands)) == 1
    assert session.sent(GetChat) == []


async def test_default_message_is_bound_and_comes_from_the_bot() -> None:
    bot = make_bot(FakeTelegramSession())
    message = await bot.send_message(chat_id=42, text="hello")
    assert message.bot is bot
    assert message.from_user is not None
    assert (message.from_user.id, message.from_user.is_bot) == (BOT_ID, True)


@pytest.mark.parametrize(
    ("chat_id", "chat_type"),
    [(42, "private"), (-5, "group"), (-1001234567890, "supergroup")],
)
async def test_default_message_derives_the_chat_type(
    chat_id: int, chat_type: str
) -> None:
    message = await make_bot(FakeTelegramSession()).send_message(
        chat_id=chat_id, text="x"
    )
    assert message.chat.type == chat_type


async def test_default_message_needs_a_numeric_chat_id() -> None:
    bot = make_bot(FakeTelegramSession())
    with pytest.raises(LookupError, match="SendMessage"):
        await bot.send_message(chat_id="@channel", text="x")


async def test_inline_edit_returns_true() -> None:
    bot = make_bot(FakeTelegramSession())
    assert await bot.edit_message_text(text="new", inline_message_id="abc") is True


async def test_stream_content_is_not_supported() -> None:
    session = FakeTelegramSession()
    with pytest.raises(LookupError, match="does not download files"):
        async for _ in session.stream_content("https://example.test/file"):
            pass


def test_queue_rejects_a_non_method_type() -> None:
    with pytest.raises(TypeError, match="Expected a TelegramMethod subclass"):
        FakeTelegramSession().queue(Message, True)


def test_callback_builder_fills_message_and_user() -> None:
    update = build_callback_update("x", user_id=7, message_text="menu")
    assert update.callback_query is not None
    assert update.callback_query.from_user.id == 7
    assert isinstance(update.callback_query.message, Message)
    assert update.callback_query.message.text == "menu"
    assert update.callback_query.message.chat.id == 7
    assert update.callback_query.message.from_user is not None
    assert update.callback_query.message.from_user.id == BOT_ID


def test_message_builder_passes_user_fields_through() -> None:
    update = build_message_update("x", first_name="Bob", is_bot=True, update_id=0)
    assert update.update_id == 0
    assert update.message is not None and update.message.from_user is not None
    assert (update.message.from_user.first_name, update.message.from_user.is_bot) == (
        "Bob",
        True,
    )


def test_my_chat_member_chat_id_follows_the_chat_type() -> None:
    private = build_my_chat_member_update("member", user_id=7)
    group = build_my_chat_member_update("kicked", user_id=7, chat_type="group")
    explicit = build_my_chat_member_update(
        "member", user_id=7, chat_id=-100, language_code="ru"
    )
    assert private.my_chat_member is not None and group.my_chat_member is not None
    assert explicit.my_chat_member is not None
    assert private.my_chat_member.chat.id == 7
    assert group.my_chat_member.chat.id == -7
    assert explicit.my_chat_member.chat.id == -100
    assert explicit.my_chat_member.from_user.language_code == "ru"


async def test_copy_returns_a_new_message_id() -> None:
    """copyMessage answers with the id of the copy, not a Message; a
    broadcast test would otherwise queue one result per recipient."""
    session = FakeTelegramSession()
    bot = make_bot(session)
    first = await bot.copy_message(chat_id=10, from_chat_id=1, message_id=7)
    second = await bot.copy_message(chat_id=20, from_chat_id=1, message_id=7)
    assert isinstance(first, MessageId)
    assert first.message_id != second.message_id
