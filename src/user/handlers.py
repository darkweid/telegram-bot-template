from collections.abc import Sequence

from aiogram import F, Router, html
from aiogram.enums import ChatType
from aiogram.filters import (
    KICKED,
    MEMBER,
    ChatMemberUpdatedFilter,
    Command,
    CommandStart,
)
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.callback_answer import CallbackAnswer
from aiogram.utils.i18n import I18n, gettext as _
from dishka import FromDishka

from src.core.telegram.commands import BotCommandSpec
from src.core.telegram.messages import safe_edit_text
from src.user.callbacks import LanguageCallback
from src.user.keyboards import LANGUAGE_NAMES, language_keyboard
from src.user.schemas import TelegramProfile
from src.user.usecases.change_membership import ChangeMembershipUseCase
from src.user.usecases.set_locale import SetLocaleUseCase


async def start(message: Message) -> None:
    name = message.from_user.first_name if message.from_user else ""
    await message.answer(
        _("Hello, {name}!\n\nSend /help to see what I can do.").format(
            name=html.quote(name)
        )
    )


async def help_(message: Message, public_commands: Sequence[BotCommandSpec]) -> None:
    """`public_commands` is the menu the composition root assembles from every
    domain (dispatcher data), so /help and the menu never disagree."""
    lines = [_("Commands:")]
    lines += [f"/{spec.command} - {_(spec.description)}" for spec in public_commands]
    await message.answer("\n".join(lines))


async def language(message: Message) -> None:
    await message.answer(_("Choose your language:"), reply_markup=language_keyboard())


async def choose_language(
    callback: CallbackQuery,
    callback_data: LanguageCallback,
    i18n: I18n,
    callback_answer: CallbackAnswer,
    set_locale: FromDishka[SetLocaleUseCase],
) -> None:
    await set_locale.execute(callback.from_user.id, callback_data.code)
    # Answer in the language just chosen, not the one this update started in.
    with i18n.use_locale(callback_data.code):
        text = _("Language: {language}").format(
            language=LANGUAGE_NAMES[callback_data.code]
        )
    if isinstance(callback.message, Message):
        await safe_edit_text(callback.message, text)
    else:
        # An old message Telegram no longer lets us edit: confirm in the
        # callback answer instead.
        callback_answer.text = text


async def bot_blocked(
    event: ChatMemberUpdated, membership: FromDishka[ChangeMembershipUseCase]
) -> None:
    """The user blocked the bot in a private chat: they become unreachable."""
    await membership.execute(
        TelegramProfile.from_aiogram(event.from_user), is_member=False
    )


async def bot_unblocked(
    event: ChatMemberUpdated, membership: FromDishka[ChangeMembershipUseCase]
) -> None:
    """The user unblocked the bot: they are reachable again."""
    await membership.execute(
        TelegramProfile.from_aiogram(event.from_user), is_member=True
    )


def build_user_router() -> Router:
    """A new router per dispatcher: aiogram attaches a router to one parent
    only, and tests build a dispatcher each."""
    router = Router(name="user")
    # Private chats only: in a group these commands would answer everyone.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.my_chat_member.filter(F.chat.type == ChatType.PRIVATE)

    router.message.register(start, CommandStart())
    router.message.register(help_, Command("help"))
    router.message.register(language, Command("language"))
    router.callback_query.register(choose_language, LanguageCallback.filter())
    router.my_chat_member.register(
        bot_blocked, ChatMemberUpdatedFilter(member_status_changed=KICKED)
    )
    router.my_chat_member.register(
        bot_unblocked, ChatMemberUpdatedFilter(member_status_changed=MEMBER)
    )
    return router
