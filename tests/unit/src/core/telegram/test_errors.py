import logging

from aiogram import Dispatcher, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import Message, Update
from aiogram.utils.i18n import I18n
from dishka.integrations.aiogram import setup_dishka
from pydantic import BaseModel, ValidationError
import pytest
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
) -> FakeTelegramSession:
    session = session or FakeTelegramSession()
    container = build_test_container(settings, session, i18n)
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
            Model(age=secret)  # type: ignore[arg-type]
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
