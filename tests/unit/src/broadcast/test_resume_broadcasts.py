from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.broadcast.repositories import BroadcastRepository
from src.broadcast.services import STALE_AFTER
from src.broadcast.task_names import RUN_BROADCAST
from src.broadcast.usecases import resume_broadcasts as resume_module
from src.broadcast.usecases.resume_broadcasts import ResumeStalledBroadcastsUseCase
from src.broadcast.usecases.stop_broadcast import StopBroadcastUseCase
from src.core.database.uow import SQLAlchemyUnitOfWork
from tests.fakes.db import FakeAsyncSession

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
FIRST = UUID("01900000-0000-7000-8000-000000000001")
SECOND = UUID("01900000-0000-7000-8000-000000000002")


async def test_stalled_broadcasts_are_queued_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that died mid-run leaves its broadcast RUNNING; nothing else
    would ever finish it."""
    monkeypatch.setattr(resume_module, "get_utc_now", lambda: NOW)
    session = FakeAsyncSession()
    broadcasts = AsyncMock(spec=BroadcastRepository)
    broadcasts.find_stalled.return_value = [FIRST, SECOND]
    use_case = ResumeStalledBroadcastsUseCase(SQLAlchemyUnitOfWork(session), broadcasts)
    assert await use_case.execute() == 2
    assert broadcasts.find_stalled.await_args.args[1] == NOW - STALE_AFTER
    queued = [
        (call.args[0].task_name, call.args[0].kwargs)
        for call in session.add.call_args_list
    ]
    assert queued == [
        (RUN_BROADCAST, {"broadcast_id": str(FIRST)}),
        (RUN_BROADCAST, {"broadcast_id": str(SECOND)}),
    ]
    session.commit.assert_awaited_once()


async def test_a_stop_is_committed() -> None:
    session = FakeAsyncSession()
    broadcasts = AsyncMock(spec=BroadcastRepository)
    broadcasts.request_stop.return_value = True
    use_case = StopBroadcastUseCase(SQLAlchemyUnitOfWork(session), broadcasts)
    assert await use_case.execute(FIRST) is True
    session.commit.assert_awaited_once()
