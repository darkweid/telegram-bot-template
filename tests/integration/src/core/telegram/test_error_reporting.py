"""A real unique violation through the real driver, reported by the bot.

asyncpg puts PostgreSQL's DETAIL into the message of its own error and of
SQLAlchemy's adapter error, so only a chain built by the libraries themselves
shows whether any of it reaches a log line or Sentry.
"""

import logging
import traceback

from aiogram import Dispatcher, Router
from aiogram.methods import SendMessage
from aiogram.types import Message
from aiogram.utils.i18n import I18n
from dishka.integrations.aiogram import setup_dishka
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors.exceptions import InfrastructureException
from src.core.telegram import errors as errors_module
from src.core.telegram.errors import build_errors_router
from src.main.config import Config
from src.user.repositories import UserRepository
from tests.factories.updates import build_message_update
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.helpers.logs import captured_logs
from tests.helpers.providers import build_test_container
from tests.integration.conftest import build_user_data, new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _duplicate_user(db_session: AsyncSession) -> tuple[int, BaseException]:
    repository = UserRepository()
    user_id = new_user_id()
    await repository.create(db_session, build_user_data(user_id))
    await db_session.flush()
    await repository.create(db_session, build_user_data(user_id))
    try:
        try:
            await db_session.flush()
        except IntegrityError as error:
            raise InfrastructureException("user write failed") from error
    except InfrastructureException as wrapped:
        return user_id, wrapped
    raise AssertionError("the second insert did not fail")


async def test_a_unique_violation_is_reported_without_its_detail(
    db_session: AsyncSession,
    settings: Config,
    i18n: I18n,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, error = await _duplicate_user(db_session)
    raw = "".join(traceback.format_exception(error))
    assert f"({user_id})" in raw  # the libraries do leak it, unscrubbed

    reported: list[BaseException] = []
    monkeypatch.setattr(errors_module.sentry_sdk, "capture_exception", reported.append)
    session = FakeTelegramSession()
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
        with captured_logs(errors_module.__name__) as records:
            await dp.feed_update(bot, build_message_update(user_id=7))
    finally:
        await dp.emit_shutdown(bot=bot)
        await container.close()

    assert len(session.sent(SendMessage)) == 1
    [record] = [r for r in records if r.levelno == logging.ERROR]
    assert record.exc_info is not None
    [event] = reported
    for text in (
        record.getMessage() + logging.Formatter().formatException(record.exc_info),
        "".join(traceback.format_exception(event)),
    ):
        assert "ScrubbedError" in text
        assert f"({user_id})" not in text
        assert "DETAIL" not in text
        assert "already exists" not in text
