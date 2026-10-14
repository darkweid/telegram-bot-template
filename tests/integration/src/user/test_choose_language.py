from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.utils.i18n import I18n
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.main.config import Config
from src.user.callbacks import LanguageCallback
from src.user.models import User
from tests.factories.updates import build_callback_update
from tests.helpers.dispatcher import running_bot
from tests.integration.conftest import new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_choosing_a_language_through_the_dispatcher_stores_it(
    integration_engine: AsyncEngine, settings: Config, i18n: I18n
) -> None:
    """UserMiddleware and the handler each run a use case in the same update,
    with the production wiring: neither may leave the other a used-up unit of
    work. The first press stores a new user, the second finds them fresh and
    writes nothing, so both of TouchUserUseCase's paths come before the
    handler."""
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    user_id = new_user_id()
    try:
        async with running_bot(settings, i18n) as running:
            for code in ("ru", "en"):
                await running.feed(
                    build_callback_update(
                        LanguageCallback(code=code).pack(), user_id=user_id
                    )
                )
            assert [edit.text for edit in running.session.sent(EditMessageText)] == [
                "Язык: Русский",
                "Language: English",
            ]
            assert len(running.session.sent(AnswerCallbackQuery)) == 2
            assert running.session.sent(SendMessage) == []
        async with sessions() as session:
            locale = await session.scalar(select(User.locale).where(User.id == user_id))
        assert locale == "en"
    finally:
        async with sessions() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
