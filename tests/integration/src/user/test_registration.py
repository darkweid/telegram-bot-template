from collections.abc import AsyncIterator, Iterator
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.cache.memory_cache import InMemoryCache
from src.core.cache.serializer import JsonSerializer
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.registry import event_subscribers, set_event_subscribers
from src.core.outbox.models import OutboxMessage
from src.main.config import Config
from src.main.event_subscribers import EVENT_SUBSCRIBERS
from src.user.models import User
from src.user.repositories import UserRepository
from src.user.schemas import TelegramProfile
from src.user.tasks import NOTIFY_ADMINS_OF_NEW_USER
from src.user.usecases.touch_user import TouchUserUseCase
from tests.integration.conftest import new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _production_subscribers() -> Iterator[None]:
    saved = event_subscribers()
    set_event_subscribers(EVENT_SUBSCRIBERS)
    yield
    set_event_subscribers(saved)


@pytest.fixture
def user_id() -> int:
    # Always past int4: the outbox filters below must compare ids as bigint,
    # and new_user_id() alone stays under 2**31 on some draws.
    return 2**31 + new_user_id()


@pytest_asyncio.fixture(loop_scope="session")
async def cleanup(integration_engine: AsyncEngine, user_id: int) -> AsyncIterator[None]:
    yield
    async with AsyncSession(integration_engine) as session, session.begin():
        await session.execute(
            # Containment, not `->> ... ::integer`: a Telegram id overflows int4.
            delete(OutboxMessage).where(
                OutboxMessage.kwargs.contains({"user_id": user_id})
            )
        )
        await session.execute(delete(User).where(User.id == user_id))


async def _touch(engine: AsyncEngine, settings: Config, user_id: int) -> None:
    profile = TelegramProfile(
        id=user_id, first_name="Ann", username=None, last_name=None, language_code="en"
    )
    async with AsyncSession(engine) as session:
        use_case = TouchUserUseCase(
            SQLAlchemyUnitOfWork(session),
            UserRepository(),
            # A fresh cache per touch: every touch reads the database.
            InMemoryCache(JsonSerializer(), default_ttl=60, version_ttl=600),
            settings.bot,
        )
        await use_case.execute(profile)


async def _notices(engine: AsyncEngine, user_id: int) -> list[OutboxMessage]:
    async with AsyncSession(engine) as session:
        rows = await session.scalars(
            select(OutboxMessage).where(
                OutboxMessage.task_name == NOTIFY_ADMINS_OF_NEW_USER,
                OutboxMessage.kwargs.contains({"user_id": user_id}),
            )
        )
        return list(rows)


async def test_a_first_touch_stores_the_user_and_the_notice_together(
    integration_engine: AsyncEngine, settings: Config, user_id: int, cleanup: None
) -> None:
    """The notice must commit with the user: a crash right after the commit
    still leaves it for the sweeper."""
    await _touch(integration_engine, settings, user_id)
    [notice] = await _notices(integration_engine, user_id)
    assert notice.kwargs == {"user_id": user_id}


async def test_a_returning_user_is_not_announced_again(
    integration_engine: AsyncEngine, settings: Config, user_id: int, cleanup: None
) -> None:
    """Only the insert is a registration; a notice per comeback would flood
    the admins."""
    await _touch(integration_engine, settings, user_id)
    # An hour back: the second touch is due a write, so it reaches the upsert,
    # which now updates the existing row.
    async with AsyncSession(integration_engine) as session, session.begin():
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(last_seen_at=User.last_seen_at - timedelta(hours=1))
        )
    await _touch(integration_engine, settings, user_id)
    assert len(await _notices(integration_engine, user_id)) == 1
