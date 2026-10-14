from datetime import UTC, datetime
from unittest.mock import AsyncMock

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    Message,
    Update,
    User,
)
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide
import pytest

from src.main.config import Config
from src.user.callbacks import LanguageCallback
from src.user.exceptions import UnsupportedLocaleError
from src.user.schemas import TelegramProfile, UserProfile
from src.user.usecases.change_membership import ChangeMembershipUseCase
from src.user.usecases.set_locale import SetLocaleUseCase
from src.user.usecases.touch_user import TouchUserUseCase
from tests.factories.updates import (
    build_callback_update,
    build_message_update,
    build_my_chat_member_update,
)
from tests.helpers.dispatcher import running_bot


class UseCases(Provider):
    """The user use cases as mocks; the stored user has `locale`."""

    def __init__(self, locale: str | None = None) -> None:
        super().__init__()
        self.touch = AsyncMock(spec=TouchUserUseCase)
        self.touch.execute.side_effect = lambda profile: UserProfile(
            id=profile.id,
            first_name=profile.first_name,
            username=profile.username,
            last_name=profile.last_name,
            language_code=profile.language_code,
            locale=locale,
            is_reachable=True,
            last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.set_locale = AsyncMock(spec=SetLocaleUseCase)
        self.change = AsyncMock(spec=ChangeMembershipUseCase)

    @provide(scope=Scope.REQUEST, override=True)
    def touch_user(self) -> TouchUserUseCase:
        return self.touch

    @provide(scope=Scope.REQUEST, override=True)
    def set_locale_use_case(self) -> SetLocaleUseCase:
        return self.set_locale

    @provide(scope=Scope.REQUEST, override=True)
    def change_membership(self) -> ChangeMembershipUseCase:
        return self.change


async def _replies(
    settings: Config,
    i18n: I18n,
    update: Update,
    use_cases: UseCases | None = None,
) -> list[SendMessage]:
    async with running_bot(settings, i18n, use_cases or UseCases()) as running:
        await running.feed(update)
        return running.session.sent(SendMessage)


async def test_start_greets_in_telegrams_language(settings: Config, i18n: I18n) -> None:
    [reply] = await _replies(
        settings, i18n, build_message_update("/start", language_code="ru")
    )
    assert reply.text is not None
    assert reply.text.startswith("Привет, Ann!")


async def test_start_escapes_the_users_name(settings: Config, i18n: I18n) -> None:
    [reply] = await _replies(
        settings, i18n, build_message_update("/start", first_name="<b>x</b>")
    )
    assert reply.text is not None
    assert "&lt;b&gt;x&lt;/b&gt;" in reply.text


async def test_the_stored_choice_beats_telegrams_language(
    settings: Config, i18n: I18n
) -> None:
    [reply] = await _replies(
        settings,
        i18n,
        build_message_update("/start", language_code="en"),
        UseCases(locale="ru"),
    )
    assert reply.text is not None
    assert reply.text.startswith("Привет")


async def test_help_lists_every_command(settings: Config, i18n: I18n) -> None:
    [reply] = await _replies(settings, i18n, build_message_update("/help"))
    assert reply.text is not None
    for command in ("/start", "/help", "/language"):
        assert command in reply.text


async def test_language_offers_every_supported_language(
    settings: Config, i18n: I18n
) -> None:
    [reply] = await _replies(settings, i18n, build_message_update("/language"))
    markup = reply.reply_markup
    assert markup is not None
    data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert data == [
        LanguageCallback(code="en").pack(),
        LanguageCallback(code="ru").pack(),
    ]


async def test_choosing_a_language_saves_it_and_answers_in_it(
    settings: Config, i18n: I18n
) -> None:
    use_cases = UseCases()
    async with running_bot(settings, i18n, use_cases) as running:
        await running.feed(
            build_callback_update(LanguageCallback(code="ru").pack(), user_id=42)
        )
        use_cases.set_locale.execute.assert_awaited_once_with(42, "ru")
        [edit] = running.session.sent(EditMessageText)
        assert edit.text == "Язык: Русский"
        assert len(running.session.sent(AnswerCallbackQuery)) == 1


async def test_an_inaccessible_message_is_confirmed_in_the_callback_answer(
    settings: Config, i18n: I18n
) -> None:
    use_cases = UseCases()
    callback = CallbackQuery(
        id="1",
        from_user=User(id=42, is_bot=False, first_name="Ann", language_code="en"),
        chat_instance="instance",
        message=InaccessibleMessage(chat=Chat(id=42, type="private"), message_id=1),
        data=LanguageCallback(code="ru").pack(),
    )
    async with running_bot(settings, i18n, use_cases) as running:
        await running.feed(Update(update_id=999_002, callback_query=callback))
        use_cases.set_locale.execute.assert_awaited_once_with(42, "ru")
        assert running.session.sent(EditMessageText) == []
        [answer] = running.session.sent(AnswerCallbackQuery)
        assert answer.text == "Язык: Русский"


async def test_an_unknown_language_is_refused_politely(
    settings: Config, i18n: I18n
) -> None:
    use_cases = UseCases()
    use_cases.set_locale.execute.side_effect = UnsupportedLocaleError()
    async with running_bot(settings, i18n, use_cases) as running:
        await running.feed(build_callback_update(LanguageCallback(code="xx").pack()))
        [reply] = running.session.sent(SendMessage)
        assert reply.text == "This language is not available."
        assert len(running.session.sent(AnswerCallbackQuery)) == 1


async def test_commands_in_a_group_are_ignored(settings: Config, i18n: I18n) -> None:
    use_cases = UseCases()
    replies = await _replies(
        settings,
        i18n,
        build_message_update("/start", chat_id=-100, chat_type="group"),
        use_cases,
    )
    assert replies == []
    use_cases.touch.execute.assert_not_awaited()


async def test_an_update_without_a_sender_passes_quietly(
    settings: Config, i18n: I18n
) -> None:
    use_cases = UseCases()
    post = Message(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=-100, type="channel"),
        text="news",
    )
    replies = await _replies(
        settings, i18n, Update(update_id=999_001, channel_post=post), use_cases
    )
    assert replies == []
    use_cases.touch.execute.assert_not_awaited()


@pytest.mark.parametrize(("status", "is_member"), [("kicked", False), ("member", True)])
async def test_membership_changes_are_recorded(
    settings: Config, i18n: I18n, status: str, is_member: bool
) -> None:
    use_cases = UseCases()
    update = build_my_chat_member_update(status, user_id=42)
    replies = await _replies(settings, i18n, update, use_cases)
    assert replies == []
    use_cases.touch.execute.assert_not_awaited()
    [call] = use_cases.change.execute.await_args_list
    assert isinstance(call.args[0], TelegramProfile)
    assert call.args[0].id == 42
    assert call.kwargs == {"is_member": is_member}


async def test_membership_in_a_group_is_not_a_user_block(
    settings: Config, i18n: I18n
) -> None:
    use_cases = UseCases()
    await _replies(
        settings,
        i18n,
        build_my_chat_member_update("kicked", chat_type="group"),
        use_cases,
    )
    use_cases.change.execute.assert_not_awaited()
