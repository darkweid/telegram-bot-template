from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.note.enums import PageDirection
from src.note.models import NOTE_TEXT_MAX_LENGTH, Note
from src.note.repositories import NoteRepository, NoteRows
from src.user.models import User
from src.user.repositories import UserRepository
from tests.integration.conftest import build_user_data, new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")

SIZE = 3


def _note_id(number: int) -> UUID:
    """Ids in a known order: keyset pages are compared by id, and uuid7 from
    the clock would make the expected order depend on timing."""
    return UUID(f"01900000-0000-7000-8000-{number:012d}")


@pytest.fixture
def user_id() -> int:
    return new_user_id()


@pytest.fixture
def other_id() -> int:
    return new_user_id()


@pytest_asyncio.fixture(loop_scope="session")
async def users(
    db_session: AsyncSession, user_id: int, other_id: int
) -> AsyncIterator[None]:
    repository = UserRepository()
    await repository.create(db_session, build_user_data(user_id))
    await repository.create(db_session, build_user_data(other_id))
    await db_session.commit()
    yield
    await db_session.rollback()
    # Notes go with their users: the foreign key cascades.
    await db_session.execute(delete(User).where(User.id.in_([user_id, other_id])))
    await db_session.commit()


async def _write(session: AsyncSession, user_id: int, *numbers: int) -> None:
    notes = NoteRepository()
    for number in numbers:
        await notes.create(
            session,
            {"id": _note_id(number), "user_id": user_id, "text": f"note {number}"},
        )
    await session.commit()


def _numbers(rows: NoteRows) -> list[int]:
    return [int(str(note.id)[-12:]) for note in rows.notes]


async def _page(
    session: AsyncSession,
    user_id: int,
    cursor: int | None = None,
    direction: PageDirection = PageDirection.OLDER,
) -> NoteRows:
    return await NoteRepository().page_for_user(
        session,
        user_id,
        cursor=None if cursor is None else _note_id(cursor),
        direction=direction,
        size=SIZE,
    )


async def test_pages_walk_from_the_newest_note_to_the_oldest(
    db_session: AsyncSession, user_id: int, users: None
) -> None:
    await _write(db_session, user_id, *range(1, 8))
    first = await _page(db_session, user_id)
    assert (_numbers(first), first.has_newer, first.has_older) == (
        [7, 6, 5],
        False,
        True,
    )
    second = await _page(db_session, user_id, cursor=5)
    assert (_numbers(second), second.has_newer, second.has_older) == (
        [4, 3, 2],
        True,
        True,
    )
    last = await _page(db_session, user_id, cursor=2)
    assert (_numbers(last), last.has_newer, last.has_older) == ([1], True, False)


async def test_newer_walks_back_to_the_first_page(
    db_session: AsyncSession, user_id: int, users: None
) -> None:
    await _write(db_session, user_id, *range(1, 8))
    back = await _page(db_session, user_id, cursor=1, direction=PageDirection.NEWER)
    assert (_numbers(back), back.has_newer, back.has_older) == ([4, 3, 2], True, True)
    top = await _page(db_session, user_id, cursor=4, direction=PageDirection.NEWER)
    assert (_numbers(top), top.has_newer, top.has_older) == ([7, 6, 5], False, True)


async def test_a_full_last_page_offers_no_older_one(
    db_session: AsyncSession, user_id: int, users: None
) -> None:
    """Exactly two pages: an "Older" button on the second would lead to an
    empty page."""
    await _write(db_session, user_id, *range(1, 7))
    last = await _page(db_session, user_id, cursor=4)
    assert (_numbers(last), last.has_newer, last.has_older) == ([3, 2, 1], True, False)


async def test_a_single_page_offers_no_navigation(
    db_session: AsyncSession, user_id: int, users: None
) -> None:
    await _write(db_session, user_id, 1, 2, 3)
    only = await _page(db_session, user_id)
    assert (_numbers(only), only.has_newer, only.has_older) == ([3, 2, 1], False, False)


async def test_a_page_holds_only_the_users_own_notes(
    db_session: AsyncSession, user_id: int, other_id: int, users: None
) -> None:
    await _write(db_session, user_id, 1, 3)
    await _write(db_session, other_id, 2, 4)
    own = await _page(db_session, user_id)
    assert (_numbers(own), own.has_newer, own.has_older) == ([3, 1], False, False)


async def test_no_notes_past_the_cursor_is_an_empty_page(
    db_session: AsyncSession, user_id: int, users: None
) -> None:
    await _write(db_session, user_id, 5)
    empty = await _page(db_session, user_id, cursor=5)
    assert (empty.notes, empty.has_newer, empty.has_older) == ([], False, False)


async def test_latest_returns_the_newest_notes_first(
    db_session: AsyncSession, user_id: int, other_id: int, users: None
) -> None:
    await _write(db_session, user_id, 1, 2, 3, 4)
    await _write(db_session, other_id, 5)
    latest = await NoteRepository().latest_for_user(db_session, user_id, 3)
    assert [note.text for note in latest] == ["note 4", "note 3", "note 2"]


async def test_someone_elses_note_is_not_deleted(
    db_session: AsyncSession, user_id: int, other_id: int, users: None
) -> None:
    """A forged callback carries any note id; the owner filter is what keeps
    one user from deleting another's note."""
    await _write(db_session, other_id, 1)
    deleted = await NoteRepository().delete(db_session, id=_note_id(1), user_id=user_id)
    await db_session.commit()
    assert deleted is None
    # A query, not session.get: the identity map would answer from memory
    # even if the row were gone.
    assert (
        await db_session.scalar(select(Note.id).where(Note.id == _note_id(1)))
        is not None
    )


async def test_a_deleted_user_takes_their_notes_along(
    db_session: AsyncSession, user_id: int, users: None
) -> None:
    await _write(db_session, user_id, 1, 2)
    await db_session.execute(delete(User).where(User.id == user_id))
    await db_session.commit()
    remaining = await db_session.scalars(select(Note).where(Note.user_id == user_id))
    assert list(remaining) == []


@pytest.mark.parametrize("text", ["", "x" * (NOTE_TEXT_MAX_LENGTH + 1)])
async def test_the_table_refuses_an_empty_or_oversized_note(
    db_session: AsyncSession, user_id: int, users: None, text: str
) -> None:
    await NoteRepository().create(
        db_session, {"id": _note_id(1), "user_id": user_id, "text": text}
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
