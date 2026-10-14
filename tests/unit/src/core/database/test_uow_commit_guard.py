from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database.base import Base
from src.core.database.mixins import IntegerIDMixin
from src.core.database.repositories import BaseRepository
from src.core.database.uow import SQLAlchemyUnitOfWork
from tests.fakes.db import FakeAsyncSession


class Note(IntegerIDMixin, Base):
    __tablename__ = "commit_guard_notes"

    title: Mapped[str] = mapped_column(String(50))


class NoteRepository(BaseRepository[Note]):
    model = Note


def _valid_note_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"title": "A note"}
    data.update(overrides)
    return data


@pytest.fixture
def session() -> FakeAsyncSession:
    return FakeAsyncSession()


@pytest.fixture
def note_repository() -> NoteRepository:
    return NoteRepository()


async def test_standalone_commit_allowed_even_after_autobegin(
    session: FakeAsyncSession, note_repository: NoteRepository
) -> None:
    # a prior read opens a transaction via autobegin; guard must not trip
    session.execute.return_value = MagicMock()
    await note_repository.get_list(session)
    created = await note_repository.create(
        session, data=_valid_note_data(), commit=True
    )
    assert created is not None


async def test_commit_true_inside_uow_raises(
    session: FakeAsyncSession, note_repository: NoteRepository
) -> None:
    uow = SQLAlchemyUnitOfWork(session)
    async with uow:
        with pytest.raises(RuntimeError, match="inside an active UnitOfWork"):
            await note_repository.create(session, data=_valid_note_data(), commit=True)
        # the UoW transaction survived the refusal
        await uow.commit()


async def test_commit_false_inside_uow_is_legal(
    session: FakeAsyncSession, note_repository: NoteRepository
) -> None:
    async with SQLAlchemyUnitOfWork(session) as uow:
        await note_repository.create(uow.session, data=_valid_note_data(), commit=False)
        await uow.commit()


async def test_marker_cleared_after_uow_exit(session: FakeAsyncSession) -> None:
    async with SQLAlchemyUnitOfWork(session):
        assert session.info.get("uow_active") is True
    assert session.info.get("uow_active") is None


async def test_marker_cleared_when_uow_body_raises(session: FakeAsyncSession) -> None:
    with pytest.raises(ValueError):
        async with SQLAlchemyUnitOfWork(session):
            raise ValueError("boom")
    assert session.info.get("uow_active") is None
