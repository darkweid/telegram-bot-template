from datetime import UTC, datetime

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.schemas import Actor, PublishedEvent
from src.core.outbox.models import OutboxMessage
from src.user.events import UserRegistered
from src.user.subscribers import enqueue_new_user_notice
from src.user.tasks import NOTIFY_ADMINS_OF_NEW_USER
from tests.fakes.db import FakeAsyncSession


async def test_a_registration_enqueues_the_admin_notice() -> None:
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session)
    published = PublishedEvent(
        event=UserRegistered(user_id=7),
        actor=Actor.telegram_user(7),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    async with uow:
        await enqueue_new_user_notice(uow, published)
        await uow.commit()
    [row] = [call.args[0] for call in session.add.call_args_list]
    assert isinstance(row, OutboxMessage)
    assert (row.task_name, row.args, row.kwargs) == (
        NOTIFY_ADMINS_OF_NEW_USER,
        [],
        {"user_id": 7},
    )
