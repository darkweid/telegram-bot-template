from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.utils.i18n import I18n

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.telegram.roles import Roles
from src.main.config import Config
from src.user.models import User
from src.user.repositories import UserRepository
from src.user.usecases.notify_admins import NotifyAdminsOfNewUserUseCase
from tests.factories.users import build_user_model
from tests.fakes.db import FakeAsyncSession
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.helpers.logs import captured_logs

NEW = 100
ADMIN_EN = 1
ADMIN_RU = 2


def _users(*stored: User) -> AsyncMock:
    by_id = {user.id: user for user in stored}
    users = AsyncMock(spec=UserRepository)
    users.get_single.side_effect = lambda _session, *, id: by_id.get(id)
    return users


def _use_case(
    settings: Config,
    i18n: I18n,
    session: FakeTelegramSession,
    users: AsyncMock,
    *admins: int,
    enabled: bool = True,
) -> NotifyAdminsOfNewUserUseCase:
    bot_config = settings.bot.model_copy(update={"NOTIFY_ADMINS_ON_NEW_USER": enabled})
    return NotifyAdminsOfNewUserUseCase(
        SQLAlchemyUnitOfWork(FakeAsyncSession()),
        users,
        make_bot(session),
        i18n,
        Roles(admins=frozenset(admins), developers=frozenset()),
        bot_config,
    )


def _stored(**new_user: object) -> tuple[User, ...]:
    fields: dict[str, object] = {
        "id": NEW,
        "first_name": "<Bob>",
        "username": "bob_smith",
    }
    return (
        build_user_model(**(fields | new_user)),
        build_user_model(id=ADMIN_EN, language_code="en"),
        build_user_model(id=ADMIN_RU, language_code="en", locale="ru"),
    )


async def test_every_admin_hears_of_a_new_user_in_their_language(
    settings: Config, i18n: I18n
) -> None:
    """The admin's own /language choice wins, as in every other reply; the
    name is the user's own text and must not break the HTML."""
    session = FakeTelegramSession()
    use_case = _use_case(
        settings, i18n, session, _users(*_stored()), ADMIN_EN, ADMIN_RU
    )
    assert await use_case.execute(NEW) == 2
    texts = {call.chat_id: call.text for call in session.sent(SendMessage)}
    assert texts[ADMIN_EN] == "New user: &lt;Bob&gt; (@bob_smith), id 100"
    assert texts[ADMIN_RU] == "Новый пользователь: &lt;Bob&gt; (@bob_smith), id 100"


async def test_a_user_without_a_username_is_named_without_one(
    settings: Config, i18n: I18n
) -> None:
    """Most Telegram users have no username; "(@None)" would be noise."""
    session = FakeTelegramSession()
    use_case = _use_case(
        settings, i18n, session, _users(*_stored(username=None)), ADMIN_EN
    )
    assert await use_case.execute(NEW) == 1
    [message] = session.sent(SendMessage)
    assert message.text == "New user: &lt;Bob&gt;, id 100"


async def test_the_switch_turns_the_notice_off(settings: Config, i18n: I18n) -> None:
    session = FakeTelegramSession()
    use_case = _use_case(
        settings, i18n, session, _users(*_stored()), ADMIN_EN, enabled=False
    )
    assert await use_case.execute(NEW) == 0
    assert session.sent(SendMessage) == []


async def test_an_admin_who_never_wrote_to_the_bot_is_skipped(
    settings: Config, i18n: I18n
) -> None:
    """Telegram refuses a first message from a bot; the call would only fail."""
    session = FakeTelegramSession()
    use_case = _use_case(settings, i18n, session, _users(*_stored()), ADMIN_EN, 3)
    assert await use_case.execute(NEW) == 1
    assert [call.chat_id for call in session.sent(SendMessage)] == [ADMIN_EN]


async def test_an_admin_who_blocked_the_bot_is_skipped(
    settings: Config, i18n: I18n
) -> None:
    """They would fail with Forbidden on every registration until they come
    back, and their next update marks them reachable again."""
    session = FakeTelegramSession()
    blocked = build_user_model(id=3, is_reachable=False)
    use_case = _use_case(
        settings, i18n, session, _users(*_stored(), blocked), ADMIN_EN, 3
    )
    assert await use_case.execute(NEW) == 1
    assert [call.chat_id for call in session.sent(SendMessage)] == [ADMIN_EN]


async def test_no_admins_configured_sends_nothing(settings: Config, i18n: I18n) -> None:
    """Most bots run without ADMIN_IDS; every registration would otherwise
    cost a database read for nobody. The only admin being the new user is the
    same case."""
    for admins in ((), (NEW,)):
        session = FakeTelegramSession()
        users = _users(*_stored())
        use_case = _use_case(settings, i18n, session, users, *admins)
        assert await use_case.execute(NEW) == 0
        assert session.sent(SendMessage) == []
        users.get_single.assert_not_awaited()


async def test_an_admin_registering_is_not_told_about_themselves(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    use_case = _use_case(settings, i18n, session, _users(*_stored()), NEW, ADMIN_EN)
    assert await use_case.execute(NEW) == 1
    assert [call.chat_id for call in session.sent(SendMessage)] == [ADMIN_EN]


async def test_one_unreachable_admin_does_not_silence_the_rest(
    settings: Config, i18n: I18n
) -> None:
    """A retry would repeat the notice to the admins who got it, so each
    admin's failure stays their own and is logged by id and error class."""
    session = FakeTelegramSession()
    session.queue(
        SendMessage,
        TelegramForbiddenError(
            method=SendMessage(chat_id=ADMIN_EN, text=""), message="blocked"
        ),
    )
    use_case = _use_case(
        settings, i18n, session, _users(*_stored()), ADMIN_EN, ADMIN_RU
    )
    with captured_logs("src.user.usecases.notify_admins") as records:
        assert await use_case.execute(NEW) == 1
    assert [message.chat_id for message in session.sent(SendMessage)] == [
        ADMIN_EN,
        ADMIN_RU,
    ]
    [record] = records
    assert record.getMessage() == (
        f"New user notice to admin {ADMIN_EN} failed: TelegramForbiddenError"
    )


async def test_a_user_gone_before_the_task_ran_is_not_announced(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    use_case = _use_case(settings, i18n, session, _users(), ADMIN_EN)
    assert await use_case.execute(NEW) == 0
    assert session.sent(SendMessage) == []
