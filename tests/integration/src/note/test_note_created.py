from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from taskiq import AsyncBroker

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.registry import event_subscribers, set_event_subscribers
from src.core.outbox.enums import OutboxMessageStatus
from src.core.outbox.models import OutboxMessage
from src.core.outbox.publisher import OutboxPublisher
from src.main.config import Config
from src.main.event_subscribers import EVENT_SUBSCRIBERS
from src.main.tasks import build_broker
from src.note.repositories import NoteRepository
from src.note.tasks import NOTE_CREATED
from src.note.usecases.create_note import CreateNoteUseCase
from src.user.models import User
from src.user.repositories import UserRepository
from tests.helpers.logs import captured_logs
from tests.integration.conftest import build_user_data, new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _production_subscribers() -> Iterator[None]:
    saved = event_subscribers()
    set_event_subscribers(EVENT_SUBSCRIBERS)
    yield
    set_event_subscribers(saved)


@pytest.fixture
def user_id() -> int:
    return new_user_id()


@pytest_asyncio.fixture(loop_scope="session")
async def author(integration_engine: AsyncEngine, user_id: int) -> AsyncIterator[None]:
    async with AsyncSession(integration_engine) as session, session.begin():
        await UserRepository().create(session, build_user_data(user_id))
    yield
    async with AsyncSession(integration_engine) as session, session.begin():
        await session.execute(
            delete(OutboxMessage).where(
                OutboxMessage.task_name == NOTE_CREATED,
                # ->> yields text; as_integer() would cast to int4, too
                # narrow for a Telegram id.
                OutboxMessage.kwargs["user_id"].as_string() == str(user_id),
            )
        )
        # The note goes with its author: the foreign key cascades.
        await session.execute(delete(User).where(User.id == user_id))


@pytest_asyncio.fixture(loop_scope="session")
async def broker(settings: Config) -> AsyncIterator[AsyncBroker]:
    """The broker the bot gets under TESTING: in memory, every task of TASKS
    registered, each run inside kick()."""
    broker = build_broker(settings)
    await broker.startup()
    yield broker
    await broker.shutdown()


async def test_a_new_note_reaches_its_task_through_the_outbox(
    integration_engine: AsyncEngine, broker: AsyncBroker, user_id: int, author: None
) -> None:
    """The production wiring end to end: if the subscriber or the task is
    missing from it, a stored note never reaches the worker."""
    publisher = OutboxPublisher(
        broker, async_sessionmaker(integration_engine, expire_on_commit=False)
    )
    with captured_logs("src.note.tasks") as records:
        async with AsyncSession(integration_engine) as session:
            use_case = CreateNoteUseCase(
                SQLAlchemyUnitOfWork(session, publisher=publisher), NoteRepository()
            )
            note_id = await use_case.execute(user_id, "Buy milk")
    [record] = records
    assert record.getMessage() == f"Note {note_id} created by user {user_id}"
    async with AsyncSession(integration_engine) as session:
        [row] = await session.scalars(
            select(OutboxMessage).where(
                OutboxMessage.task_name == NOTE_CREATED,
                OutboxMessage.kwargs["user_id"].as_string() == str(user_id),
            )
        )
    assert row.kwargs == {"note_id": str(note_id), "user_id": user_id}
    assert row.status is OutboxMessageStatus.PUBLISHED
