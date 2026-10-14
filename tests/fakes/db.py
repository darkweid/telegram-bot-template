from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import InvalidRequestError


class AsyncTransactionContext:
    """Stands in for AsyncSessionTransaction: awaitable like the real
    `session.begin()` / `begin_nested()`, usable as an async CM."""

    def __init__(self, session: FakeAsyncSession, *, nested: bool) -> None:
        self._session = session
        self.nested = nested
        self._started = False

    async def start(self) -> AsyncTransactionContext:
        if self._started:
            return self
        self._started = True
        if self.nested:
            self._session.open_savepoint(self)
        else:
            self._session.begin_top_level(self)
        return self

    def _close(self) -> None:
        if self.nested:
            self._session.close_savepoint(self)
        else:
            self._session.end_top_level(self)

    def __await__(self) -> Generator[Any, None, AsyncTransactionContext]:
        return self.start().__await__()

    async def __aenter__(self) -> AsyncTransactionContext:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        self._close()
        return None

    async def rollback(self) -> None:
        if self.nested:
            await self._session.rollback_savepoint()
        else:
            await self._session.rollback()
        self._close()


class FakeAsyncSession:
    def __init__(self, in_transaction: bool = False) -> None:
        self._in_transaction = in_transaction
        # Open `begin_nested()` contexts, innermost last. `session.commit()` /
        # `rollback()` end the whole transaction and every savepoint in it, as
        # on a real session.
        self._savepoints: list[AsyncTransactionContext] = []
        self._top_level: AsyncTransactionContext | None = None
        # Set while a savepoint forwards its rollback to the `session.rollback`
        # mock (tests assert on that mock); that one call ends only the
        # savepoint, never the transaction around it.
        self._rolling_back_savepoint = False
        self.commit = AsyncMock(side_effect=self._end_transaction)
        self.rollback = AsyncMock(side_effect=self._end_transaction)
        self.flush = AsyncMock()
        self.refresh = AsyncMock()
        self.execute = AsyncMock()
        self.add = MagicMock()
        self.delete = AsyncMock()
        # `detached_read` detaches what it loaded from the session.
        self.expunge = MagicMock()
        # Mirrors real `AsyncSession.info`: a plain dict the UoW uses to mark
        # itself active for the repository commit guard.
        self.info: dict[str, Any] = {}
        # Mirror the real session's pending-state views, consulted by the UoW's
        # forgotten-commit warning on clean exit.
        self.dirty: set[Any] = set()
        self.new: set[Any] = set()
        self.deleted: set[Any] = set()

    def in_transaction(self) -> bool:
        return self._in_transaction

    def set_in_transaction(self, value: bool) -> None:
        self._in_transaction = value
        if not value:
            self._savepoints.clear()
            self._top_level = None

    @property
    def open_savepoints(self) -> int:
        return len(self._savepoints)

    def _end_transaction(self) -> None:
        if self._rolling_back_savepoint:
            return
        self._in_transaction = False
        self._savepoints.clear()
        self._top_level = None

    def begin_top_level(self, transaction: AsyncTransactionContext) -> None:
        if self._in_transaction:
            raise InvalidRequestError("A transaction is already begun on this Session.")
        self._in_transaction = True
        self._top_level = transaction

    def end_top_level(self, transaction: AsyncTransactionContext) -> None:
        if self._top_level is transaction:
            self._in_transaction = False
            self._top_level = None

    def open_savepoint(self, savepoint: AsyncTransactionContext) -> None:
        # On an idle session the real `begin_nested()` autobegins the outer
        # transaction, which stays open after the savepoint ends.
        self._in_transaction = True
        self._savepoints.append(savepoint)

    def close_savepoint(self, savepoint: AsyncTransactionContext) -> None:
        if savepoint in self._savepoints:
            self._savepoints.remove(savepoint)

    async def rollback_savepoint(self) -> None:
        self._rolling_back_savepoint = True
        try:
            await self.rollback()
        finally:
            self._rolling_back_savepoint = False

    def begin(self) -> AsyncTransactionContext:
        return AsyncTransactionContext(self, nested=False)

    def begin_nested(self) -> AsyncTransactionContext:
        return AsyncTransactionContext(self, nested=True)

    async def __aenter__(self) -> FakeAsyncSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        return None


class FakeSessionFactory:
    """Stands in for async_sessionmaker: calling it yields the same fake session."""

    def __init__(self, session: FakeAsyncSession | None = None) -> None:
        self.session = session or FakeAsyncSession()

    def __call__(self) -> FakeAsyncSession:
        return self.session
