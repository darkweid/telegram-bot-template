import pytest
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database.base import Base
from src.core.database.detached import detached_read
from src.core.database.mixins import IntegerIDMixin
from tests.fakes.db import FakeAsyncSession


class Note(IntegerIDMixin, Base):
    __tablename__ = "detached_notes"

    title: Mapped[str] = mapped_column(String(50))


async def test_ends_the_read_transaction_it_opened() -> None:
    """Left open, it pins a pooled connection idle in transaction for the
    rest of the update, however long the handler then waits."""
    session = FakeAsyncSession()

    async with detached_read(session) as read:
        read.detach(Note(title="A note"), None)

    session.rollback.assert_awaited_once()


async def test_ends_it_when_the_block_raises() -> None:
    """An error raised after the read must not leave the connection held
    until the handler has dealt with it."""
    session = FakeAsyncSession()

    with pytest.raises(LookupError):
        async with detached_read(session):
            raise LookupError

    session.rollback.assert_awaited_once()


async def test_leaves_a_transaction_it_did_not_open() -> None:
    """Rolling back someone else's transaction would discard their work."""
    session = FakeAsyncSession(in_transaction=True)

    async with detached_read(session) as read:
        read.detach(Note(title="A note"))

    session.rollback.assert_not_awaited()


async def test_an_instance_never_attached_is_not_expunged() -> None:
    """Expunging an instance the session does not hold raises; a factory-built
    or already detached one passes through untouched."""
    session = FakeAsyncSession()

    async with detached_read(session) as read:
        read.detach(Note(title="A note"))

    session.expunge.assert_not_called()


async def test_ends_the_transaction_when_detaching_fails() -> None:
    """A failure while walking the detached graph must not also leave the
    connection held idle in transaction until the handler returns."""
    session = FakeAsyncSession()

    with pytest.raises(AttributeError):
        async with detached_read(session) as read:
            read.detach(object())

    session.rollback.assert_awaited_once()
