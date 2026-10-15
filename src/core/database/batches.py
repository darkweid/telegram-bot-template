from collections.abc import Awaitable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.uow import SQLAlchemyUnitOfWork


class DeleteBatch(Protocol):
    def __call__(self, session: AsyncSession, *, limit: int) -> Awaitable[int]:
        """Delete at most `limit` rows; returns how many went."""
        ...


async def delete_in_batches(
    session: AsyncSession,
    delete_batch: DeleteBatch,
    batch_size: int,
) -> int:
    """Run `delete_batch(session, limit=batch_size)` until a batch comes back short;
    returns the total number of rows deleted.

    Each batch commits in a unit of work of its own: in one transaction a
    failure on a late batch would roll back every earlier one, and a large
    backlog would hold its locks for the whole run.
    """
    deleted = 0
    while True:
        async with SQLAlchemyUnitOfWork(session) as uow:
            batch = await delete_batch(uow.session, limit=batch_size)
            await uow.commit()
        deleted += batch
        if batch < batch_size:
            return deleted
