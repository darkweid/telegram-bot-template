from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.broadcast.exceptions import BroadcastInProgressError
from src.broadcast.repositories import BroadcastRepository
from src.broadcast.task_names import RUN_BROADCAST
from src.broadcast.usecases import start_broadcast as start_module
from src.broadcast.usecases.start_broadcast import StartBroadcastUseCase
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.outbox.models import OutboxMessage
from src.user.repositories import UserRepository
from tests.fakes.db import FakeAsyncSession

ADMIN = 1
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_module, "get_utc_now", lambda: NOW)


def _users(count: int) -> AsyncMock:
    users = AsyncMock(spec=UserRepository)
    users.count_recipients.return_value = count
    return users


def _broadcasts(*, locked: bool = True, active: bool = False) -> AsyncMock:
    broadcasts = AsyncMock(spec=BroadcastRepository)
    broadcasts.try_xact_lock.return_value = locked
    broadcasts.has_active.return_value = active
    return broadcasts


async def _start(
    session: FakeAsyncSession, broadcasts: AsyncMock, users: AsyncMock
) -> UUID:
    use_case = StartBroadcastUseCase(SQLAlchemyUnitOfWork(session), broadcasts, users)
    return await use_case.execute(
        admin_id=ADMIN, source_chat_id=ADMIN, source_message_id=7, progress_message_id=8
    )


async def test_a_broadcast_is_stored_and_its_task_queued_together() -> None:
    """The row and the outbox row commit together: a broadcast without its task
    would block every later one, a task without its row would find nothing."""
    session = FakeAsyncSession()
    broadcasts = _broadcasts()
    users = _users(5)
    broadcast_id = await _start(session, broadcasts, users)
    [call] = broadcasts.create.await_args_list
    data = call.args[1]
    assert data == {
        "id": broadcast_id,
        "admin_id": ADMIN,
        "source_chat_id": ADMIN,
        "source_message_id": 7,
        "progress_message_id": 8,
        "total": 5,
        "created_at": NOW,
    }
    # The total and the run's walk share one cutoff, so the progress never
    # passes the total.
    assert users.count_recipients.await_args.kwargs == {"registered_by": NOW}
    [row] = [call.args[0] for call in session.add.call_args_list]
    assert isinstance(row, OutboxMessage)
    assert (row.task_name, row.args, row.kwargs) == (
        RUN_BROADCAST,
        [],
        {"broadcast_id": str(broadcast_id)},
    )
    session.commit.assert_awaited_once()


@pytest.mark.parametrize(
    ("locked", "active"),
    [(False, False), (True, True)],
    ids=["another admin is starting one", "one is already running"],
)
async def test_a_second_broadcast_is_refused(locked: bool, active: bool) -> None:
    session = FakeAsyncSession()
    broadcasts = _broadcasts(locked=locked, active=active)
    with pytest.raises(BroadcastInProgressError):
        await _start(session, broadcasts, _users(5))
    broadcasts.create.assert_not_awaited()
    session.commit.assert_not_awaited()
