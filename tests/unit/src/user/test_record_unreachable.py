from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.user.repositories import UserRepository
from src.user.usecases.record_unreachable import RecordUnreachableUseCase
from tests.fakes.db import FakeSessionFactory


async def test_marks_the_user_unreachable_in_its_own_transaction() -> None:
    sessions = FakeSessionFactory()
    users = AsyncMock(spec=UserRepository)
    users.set_reachable.return_value = True
    calls = AsyncMock()
    calls.attach_mock(users.set_reachable, "set_reachable")
    calls.attach_mock(sessions.session.commit, "commit")
    await RecordUnreachableUseCase(sessions, users).record_unreachable(42)
    users.set_reachable.assert_awaited_once()
    args = users.set_reachable.await_args.args
    assert args[1:3] == (42, False)
    assert isinstance(args[3], datetime)
    assert args[3].tzinfo is not None
    sessions.session.commit.assert_awaited_once()
    assert [c[0] for c in calls.mock_calls] == ["set_reachable", "commit"]


async def test_a_user_not_stored_yet_is_not_an_error() -> None:
    sessions = FakeSessionFactory()
    users = AsyncMock(spec=UserRepository)
    users.set_reachable.return_value = False
    await RecordUnreachableUseCase(sessions, users).record_unreachable(42)
    users.set_reachable.assert_awaited_once()
    sessions.session.commit.assert_awaited_once()


async def test_nothing_is_committed_when_the_update_fails() -> None:
    sessions = FakeSessionFactory()
    users = AsyncMock(spec=UserRepository)
    users.set_reachable.side_effect = ConnectionError("database down")
    with pytest.raises(ConnectionError):
        await RecordUnreachableUseCase(sessions, users).record_unreachable(42)
    sessions.session.commit.assert_not_awaited()
