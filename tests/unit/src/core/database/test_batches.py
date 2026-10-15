from unittest.mock import AsyncMock

import pytest

from src.core.database.batches import delete_in_batches
from tests.fakes.db import FakeAsyncSession


async def test_batches_run_until_a_short_one_and_each_commits() -> None:
    """A full batch means more rows may be left; a short one that none are.
    Each batch is its own transaction so a backlog never holds locks for the
    whole run."""
    session = FakeAsyncSession()
    delete_batch = AsyncMock(side_effect=[3, 3, 1])

    deleted = await delete_in_batches(session, delete_batch, 3)

    assert deleted == 7
    assert [call.args for call in delete_batch.await_args_list] == [(session,)] * 3
    assert all(call.kwargs == {"limit": 3} for call in delete_batch.await_args_list)
    assert session.commit.await_count == 3


async def test_an_exact_multiple_costs_one_empty_batch() -> None:
    """The last full batch cannot say whether rows remain; the empty one does."""
    session = FakeAsyncSession()
    delete_batch = AsyncMock(side_effect=[2, 0])

    assert await delete_in_batches(session, delete_batch, 2) == 2


async def test_a_failed_batch_keeps_the_earlier_ones_committed() -> None:
    """In one transaction a late failure would undo every earlier batch."""
    session = FakeAsyncSession()
    delete_batch = AsyncMock(side_effect=[2, 2, ConnectionError("database gone")])

    with pytest.raises(ConnectionError):
        await delete_in_batches(session, delete_batch, 2)

    assert session.commit.await_count == 2
    session.rollback.assert_awaited()
