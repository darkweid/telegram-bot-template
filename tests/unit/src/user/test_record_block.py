from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.user.repositories import UserRepository
from src.user.usecases.record_block import RecordBlockUseCase
from tests.fakes.db import FakeSessionFactory


async def test_marks_the_user_blocked_in_its_own_transaction() -> None:
    sessions = FakeSessionFactory()
    users = AsyncMock(spec=UserRepository)
    users.set_blocked.return_value = True
    calls = AsyncMock()
    calls.attach_mock(users.set_blocked, "set_blocked")
    calls.attach_mock(sessions.session.commit, "commit")
    await RecordBlockUseCase(sessions, users).record_blocked(42)  # type: ignore[arg-type]
    users.set_blocked.assert_awaited_once()
    args = users.set_blocked.await_args.args
    assert args[1:3] == (42, True)
    assert isinstance(args[3], datetime)
    assert args[3].tzinfo is not None
    sessions.session.commit.assert_awaited_once()
    assert [c[0] for c in calls.mock_calls] == ["set_blocked", "commit"]


async def test_a_user_not_stored_yet_is_not_an_error() -> None:
    sessions = FakeSessionFactory()
    users = AsyncMock(spec=UserRepository)
    users.set_blocked.return_value = False
    await RecordBlockUseCase(sessions, users).record_blocked(42)  # type: ignore[arg-type]
    users.set_blocked.assert_awaited_once()
    sessions.session.commit.assert_awaited_once()


async def test_nothing_is_committed_when_the_update_fails() -> None:
    sessions = FakeSessionFactory()
    users = AsyncMock(spec=UserRepository)
    users.set_blocked.side_effect = ConnectionError("database down")
    with pytest.raises(ConnectionError):
        await RecordBlockUseCase(sessions, users).record_blocked(42)  # type: ignore[arg-type]
    sessions.session.commit.assert_not_awaited()
