import logging
import traceback
from types import SimpleNamespace

from aiogram import Dispatcher, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import Message, Update
from aiogram.utils.i18n import I18n
import asyncpg
from dishka.integrations.aiogram import setup_dishka
from pydantic import BaseModel, ValidationError
import pytest
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import IntegrityError

from src.core.errors.exceptions import CoreException, InfrastructureException
from src.core.errors.scrubbed import ScrubbedError
from src.core.i18n import N_
from src.core.telegram import errors as errors_module
from src.core.telegram.errors import build_errors_router
from src.main.config import Config
from tests.factories.updates import build_message_update
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.helpers.logs import captured_logs
from tests.helpers.providers import build_test_container

GENERIC_RU = "Что-то пошло не так. Попробуйте ещё раз чуть позже."


class Captured:
    def __init__(self) -> None:
        self.errors: list[BaseException] = []

    def __call__(self, error: BaseException) -> None:
        self.errors.append(error)


@pytest.fixture
def sentry(monkeypatch: pytest.MonkeyPatch) -> Captured:
    captured = Captured()
    monkeypatch.setattr(errors_module.sentry_sdk, "capture_exception", captured)
    return captured


async def _raise(
    settings: Config,
    i18n: I18n,
    error: BaseException,
    update: Update | None = None,
    *,
    session: FakeTelegramSession | None = None,
    alert_session: FakeTelegramSession | None = None,
) -> FakeTelegramSession:
    session = session or FakeTelegramSession()
    container = build_test_container(
        settings, session, i18n, alert_session=alert_session
    )
    dp = Dispatcher()
    setup_dishka(container, router=dp, auto_inject=True)
    failing = Router()

    @failing.message()
    async def handler(message: Message) -> None:
        raise error

    dp.include_routers(build_errors_router(), failing)
    bot = make_bot(session)
    await dp.emit_startup(bot=bot)
    try:
        await dp.feed_update(
            bot, update or build_message_update(user_id=7, language_code="ru")
        )
    finally:
        await dp.emit_shutdown(bot=bot)
        await container.close()
    return session


async def test_unexpected_error_is_logged_captured_and_answered(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    error = RuntimeError("boom")
    with captured_logs(errors_module.__name__) as records:
        session = await _raise(settings, i18n, error)
    assert sentry.errors == [error]
    assert any(record.exc_info and record.exc_info[1] is error for record in records)
    [reply] = session.sent(SendMessage)
    assert (reply.chat_id, reply.text) == (7, GENERIC_RU)


async def test_core_exception_uses_its_own_level_and_message(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    class Quiet(CoreException):
        log_level = "warning"

    with captured_logs(errors_module.__name__) as records:
        session = await _raise(settings, i18n, Quiet("details for the log"))
    assert sentry.errors == []
    [record] = records
    assert record.levelno == logging.WARNING
    assert "details for the log" in record.getMessage()
    assert record.exc_info is None
    assert session.sent(SendMessage)[0].text == GENERIC_RU


async def test_infrastructure_exception_reaches_sentry(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    error = InfrastructureException("redis timed out")
    with captured_logs(errors_module.__name__) as records:
        await _raise(settings, i18n, error)
    assert sentry.errors == [error]
    [record] = records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is error


async def test_forbidden_is_logged_quietly_and_not_answered(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    error = TelegramForbiddenError(
        SendMessage(chat_id=7, text="x"), "bot was blocked by the user"
    )
    with captured_logs(errors_module.__name__) as records:
        session = await _raise(settings, i18n, error)
    assert sentry.errors == []
    assert session.sent(SendMessage) == []
    [record] = records
    assert record.levelno == logging.INFO
    assert record.exc_info is None


async def test_reply_language_follows_telegram_and_falls_back_to_default(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    session = await _raise(
        settings,
        i18n,
        RuntimeError(),
        build_message_update(user_id=7, language_code="de"),
    )
    # The default locale is en, whose catalog leaves the msgid as it is.
    assert session.sent(SendMessage)[0].text == CoreException.message_key


async def test_a_failing_reply_does_not_escape(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    session = FakeTelegramSession()
    session.queue(
        SendMessage, TelegramForbiddenError(SendMessage(chat_id=7, text="x"), "blocked")
    )
    with captured_logs(errors_module.__name__) as records:
        await _raise(settings, i18n, RuntimeError("boom"), session=session)
    assert len(session.sent(SendMessage)) == 1
    assert [record.levelno for record in records].count(logging.WARNING) == 1


async def test_errors_that_echo_user_data_are_scrubbed(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    class Model(BaseModel):
        age: int

    # Kept out of the source line: a traceback prints the line it points at.
    secret = "secret-value"
    with captured_logs(errors_module.__name__) as records:
        try:
            Model(age=secret)
        except ValidationError as validation_error:
            await _raise(settings, i18n, validation_error)
        await _raise(
            settings,
            i18n,
            IntegrityError("INSERT ...", {}, Exception("Key (username)=(ann) exists")),
        )
    assert len(sentry.errors) == 2
    assert all(isinstance(error, ScrubbedError) for error in sentry.errors)
    assert all(secret not in str(e) and "ann" not in str(e) for e in sentry.errors)

    assert len(records) == 2
    formatter = logging.Formatter()
    for record in records:
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], ScrubbedError)
        rendered = record.getMessage() + formatter.formatException(record.exc_info)
        assert secret not in rendered
        assert "(ann)" not in rendered


class Greeting(CoreException):
    message_key = N_("Order {number} is not available")
    log_level = "warning"


async def test_core_exception_params_fill_the_reply_but_stay_out_of_the_log(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    with captured_logs(errors_module.__name__) as records:
        session = await _raise(
            settings, i18n, Greeting("order lookup failed", number="4711-secret")
        )
    [reply] = session.sent(SendMessage)
    assert reply.text == "Order 4711-secret is not available"
    assert len(records) == 1
    assert "4711-secret" not in records[0].getMessage()


async def test_core_exception_params_are_escaped_for_html(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    session = await _raise(settings, i18n, Greeting(number="<b>&"))
    [reply] = session.sent(SendMessage)
    assert reply.text == "Order &lt;b&gt;&amp; is not available"


async def test_every_field_of_the_reply_is_escaped(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    class Sender:
        first_name = "<i>Ann</i>"

    class Limit(CoreException):
        message_key = "{user.first_name}: {count} of {limit:03d}"

    session = await _raise(settings, i18n, Limit(user=Sender(), count=3, limit=5))
    [reply] = session.sent(SendMessage)
    assert reply.text == "&lt;i&gt;Ann&lt;/i&gt;: 3 of 005"


def _unique_violation() -> InfrastructureException:
    """The chain asyncpg and SQLAlchemy build for a duplicate key, wrapped the
    way a use case would; every link below SQLAlchemy's repeats the DETAIL."""
    driver_error = asyncpg.exceptions.UniqueViolationError.new(
        {
            "S": "ERROR",
            "C": "23505",
            "M": 'duplicate key value violates unique constraint "uq_users_username"',
            "D": "Key (username)=(ann) already exists.",
        }
    )
    try:
        try:
            try:
                raise driver_error
            except asyncpg.PostgresError as error:
                raise AsyncAdapt_asyncpg_dbapi.IntegrityError(
                    f"{type(error)}: {error}"
                ) from error
        except AsyncAdapt_asyncpg_dbapi.IntegrityError as adapted:
            raise IntegrityError("INSERT ...", {}, adapted) from adapted
    except IntegrityError as integrity_error:
        try:
            raise InfrastructureException("user write failed") from integrity_error
        except InfrastructureException as wrapped:
            return wrapped


async def test_user_data_anywhere_in_the_chain_is_reported_scrubbed(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    class Model(BaseModel):
        age: int

    secret = "secret-value"
    with captured_logs(errors_module.__name__) as records:
        try:
            Model(age=secret)
        except ValidationError as validation_error:
            # Below a scrubbed link the chain goes on.
            validation_error.__context__ = LookupError("cache miss below")
            error = InfrastructureException("profile parse failed")
            error.__cause__ = validation_error
            await _raise(settings, i18n, error)
        outer = RuntimeError("retry gave up")
        outer.__cause__ = outer.__context__ = _unique_violation()
        await _raise(settings, i18n, outer)

    assert len(sentry.errors) == 2
    formatter = logging.Formatter()
    errors = [record for record in records if record.levelno == logging.ERROR]
    assert len(errors) == 2
    for record, event, kept in zip(
        errors, sentry.errors, ["cache miss below", "user write failed"], strict=True
    ):
        assert record.exc_info is not None
        rendered = record.getMessage() + formatter.formatException(record.exc_info)
        chain = "".join(traceback.format_exception(event))
        for text in (rendered, chain):
            assert "Traceback" in text
            assert "ScrubbedError" in text
            assert kept in text
            assert secret not in text
            assert "input_value" not in text
            assert "DETAIL" not in text
            assert "(ann)" not in text
        links: list[BaseException] = []
        pending: list[BaseException | None] = [event]
        while pending:
            link = pending.pop()
            if link is not None:
                links.append(link)
                pending += [link.__cause__, link.__context__]
        assert not any(
            isinstance(link, ValidationError | IntegrityError) for link in links
        )
        assert not any("DETAIL" in str(link) for link in links)


async def test_a_missing_param_falls_back_to_the_generic_reply(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    with captured_logs(errors_module.__name__) as records:
        session = await _raise(settings, i18n, Greeting("order lookup failed"))
    [reply] = session.sent(SendMessage)
    assert reply.text == GENERIC_RU
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert Greeting.message_key in warnings[1].getMessage()


async def test_a_param_the_template_cannot_read_falls_back_to_the_generic_reply(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    class Profile(CoreException):
        message_key = "{user.nickname} and {count:d}"
        log_level = "debug"

    # A missing attribute, then a value that does not fit its format spec.
    named = SimpleNamespace(nickname="ann")
    for error in (Profile(user=object(), count=1), Profile(user=named, count="x")):
        with captured_logs(errors_module.__name__) as records:
            session = await _raise(settings, i18n, error)
        [reply] = session.sent(SendMessage)
        assert reply.text == GENERIC_RU
        [warning] = [r for r in records if r.levelno == logging.WARNING]
        assert Profile.message_key in warning.getMessage()


def _with_developer(settings: Config, developer: int) -> Config:
    return settings.model_copy(
        update={
            "bot": settings.bot.model_copy(
                update={"DEVELOPER_IDS": frozenset({developer})}
            )
        }
    )


async def test_an_unexpected_error_alerts_the_developers(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    alerts = FakeTelegramSession()
    session = await _raise(
        _with_developer(settings, 99), i18n, RuntimeError("boom"), alert_session=alerts
    )
    # The user is answered by the handlers' Bot, the developers by the alert one.
    assert [sent.chat_id for sent in session.sent(SendMessage)] == [7]
    [alert] = alerts.sent(SendMessage)
    assert alert.chat_id == 99
    assert "RuntimeError" in alert.text
    assert "update message" in alert.text
    assert "user 7" in alert.text


async def test_the_alert_carries_ids_and_never_what_the_user_sent(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    """The alert chat is read by people who must not see users' messages or
    names; the user id is enough to find the rest."""
    update = build_message_update(
        "my secret text", user_id=7, first_name="Ann Example", language_code="ru"
    )
    alerts = FakeTelegramSession()
    await _raise(
        _with_developer(settings, 99),
        i18n,
        RuntimeError("boom"),
        update,
        alert_session=alerts,
    )
    [alert] = alerts.sent(SendMessage)
    assert "my secret text" not in alert.text
    assert "Ann Example" not in alert.text


async def test_a_core_exception_does_not_alert(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    """A CoreException is an expected refusal: the user is told, developers
    are not paged."""
    alerts = FakeTelegramSession()
    session = await _raise(
        _with_developer(settings, 99), i18n, CoreException(), alert_session=alerts
    )
    assert [sent.chat_id for sent in session.sent(SendMessage)] == [7]
    assert alerts.sent(SendMessage) == []


async def test_the_alert_carries_the_sentry_event_id(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        errors_module.sentry_sdk, "capture_exception", lambda error: "event-1"
    )
    alerts = FakeTelegramSession()
    await _raise(
        _with_developer(settings, 99), i18n, RuntimeError("boom"), alert_session=alerts
    )
    [alert] = alerts.sent(SendMessage)
    assert "sentry event-1" in alert.text


async def test_a_driver_error_reaches_the_alert_scrubbed(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    """The alert chat is one more place PostgreSQL's DETAIL must not reach."""
    alerts = FakeTelegramSession()
    await _raise(
        _with_developer(settings, 99),
        i18n,
        IntegrityError("INSERT ...", {}, Exception("Key (username)=(ann) exists")),
        alert_session=alerts,
    )
    [alert] = alerts.sent(SendMessage)
    assert "(ann)" not in alert.text


async def test_a_blocked_bot_does_not_alert(
    settings: Config, i18n: I18n, sentry: Captured
) -> None:
    """A user who blocked the bot is nothing to fix; paging developers for it
    would bury the alerts that are."""
    alerts = FakeTelegramSession()
    error = TelegramForbiddenError(
        SendMessage(chat_id=7, text="x"), "bot was blocked by the user"
    )
    await _raise(_with_developer(settings, 99), i18n, error, alert_session=alerts)
    assert alerts.sent(SendMessage) == []
