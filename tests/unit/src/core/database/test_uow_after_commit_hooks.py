import traceback
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.database.uow import SQLAlchemyUnitOfWork
from tests.fakes.db import FakeAsyncSession
from tests.helpers.logs import captured_logs


@pytest.fixture
def uow() -> SQLAlchemyUnitOfWork:
    return SQLAlchemyUnitOfWork(FakeAsyncSession())


async def test_hooks_run_in_order_after_commit(uow: SQLAlchemyUnitOfWork) -> None:
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    async def second() -> None:
        order.append("second")

    async with uow:
        uow.add_after_commit_hook(first)
        uow.add_after_commit_hook(second)
        assert order == []  # nothing fires before commit
        await uow.commit()

    assert order == ["first", "second"]


async def test_hooks_do_not_run_on_rollback(uow: SQLAlchemyUnitOfWork) -> None:
    hook = AsyncMock()
    async with uow:
        uow.add_after_commit_hook(hook)
        await uow.rollback()
    hook.assert_not_awaited()


async def test_hooks_do_not_run_on_exception_rollback(
    uow: SQLAlchemyUnitOfWork,
) -> None:
    hook = AsyncMock()
    with pytest.raises(RuntimeError, match="boom"):
        async with uow:
            uow.add_after_commit_hook(hook)
            raise RuntimeError("boom")
    hook.assert_not_awaited()


async def test_failing_hook_is_logged_and_others_still_run(
    uow: SQLAlchemyUnitOfWork,
) -> None:
    ran: list[str] = []

    async def broken() -> None:
        raise RuntimeError("hook failed")

    async def healthy() -> None:
        ran.append("healthy")

    async with uow:
        uow.add_after_commit_hook(broken)
        uow.add_after_commit_hook(healthy)
        await uow.commit()  # must not raise

    assert ran == ["healthy"]


async def test_add_hook_after_completion_raises(uow: SQLAlchemyUnitOfWork) -> None:
    async with uow:
        await uow.commit()
    with pytest.raises(RuntimeError):
        uow.add_after_commit_hook(AsyncMock())


async def test_a_failed_hook_reaches_the_log_scrubbed() -> None:
    """A hook that writes (the outbox publisher marking its row) can fail
    with a driver error that carries PostgreSQL's DETAIL."""
    uow = SQLAlchemyUnitOfWork(FakeAsyncSession())

    async with uow:
        uow.add_after_commit_hook(_raise_integrity_error)
        with captured_logs("src.core.database.uow") as records:
            await uow.commit()
    [record] = records
    assert record.exc_info is not None
    rendered = "".join(traceback.format_exception(*record.exc_info))
    assert "secret@example.com" not in rendered


async def _raise_integrity_error() -> None:
    raise IntegrityError(
        "INSERT ...",
        {"email": "secret@example.com"},
        Exception("DETAIL:  Key (email)=(secret@example.com) already exists."),
    )
