"""Repository behaviour that only real SQL can prove.

The `ilike` search `ListQuery` builds and the primary-key tiebreaker appended to every
ORDER BY are properties of the emitted statement, not of the Python around it - a fake
session would assert nothing about them.
"""

import secrets
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.query import ListQuery
from src.core.database.repositories import BaseRepository
from src.user.models import User
from src.user.repositories import UserRepository
from tests.integration.conftest import build_user_data, new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


class SearchableUserRepository(BaseRepository[User]):
    """`UserRepository` with the list-query columns opted in.

    The shipped repository declares none, deliberately: `search` and `order_by` can come
    from user input, so a domain has to allow a column before anyone can reach it. That
    leaves the SQL `ListQuery` builds with nowhere to be exercised, so the opt-in lives
    here instead of being added to the template's default.
    """

    model = User
    searchable_fields = ("first_name", "last_name", "username")
    sortable_fields = ("username", "created_at")
    default_order_by = "created_at"


def _user(username: str, **overrides: Any) -> dict[str, Any]:
    return build_user_data(
        new_user_id(), last_name="Lovelace", username=username, **overrides
    )


async def test_create_read_update_and_delete(db_session: AsyncSession) -> None:
    repository = UserRepository()
    username = f"crud-{secrets.token_hex(4)}"

    created = await repository.create(db_session, _user(username))
    await db_session.flush()

    fetched = await repository.get_single(db_session, id=created.id)
    assert fetched is not None
    assert fetched.username == username
    assert await repository.count(db_session, username=username) == 1
    assert [
        user.id for user in await repository.get_list(db_session, username=username)
    ] == [created.id]

    updated = await repository.update(
        db_session, {"first_name": "Grace"}, id=created.id
    )
    await db_session.flush()
    assert updated is not None
    assert updated.first_name == "Grace"

    deleted = await repository.delete(db_session, id=created.id)
    await db_session.flush()
    assert deleted is not None

    assert await repository.get_single(db_session, id=created.id) is None
    assert await repository.exists(db_session, id=created.id) is False
    assert await repository.count(db_session, username=username) == 0


async def test_search_matches_substrings_case_insensitively(
    db_session: AsyncSession,
) -> None:
    repository = SearchableUserRepository()
    marker = secrets.token_hex(4)
    matching = [f"zoe{marker}", f"zoe{marker}x"]
    for username in [*matching, f"other{marker}"]:
        await repository.create(db_session, _user(username))
    await db_session.flush()

    items, total = await repository.get_paginated_list(
        db_session,
        page=1,
        size=10,
        query=ListQuery(search=f"ZOE{marker}"),
    )

    assert total == 2
    assert {user.username for user in items} == set(matching)


async def test_pagination_over_a_non_unique_sort_column_is_stable(
    db_session: AsyncSession,
) -> None:
    """Every row shares one `created_at`; only the tiebreaker keeps the pages disjoint.

    `created_at` defaults to `now()`, which PostgreSQL freezes at the start of the
    transaction: all five rows below are inserted with the identical value. Ordering by
    it alone leaves the row order undefined, and limit/offset pagination would duplicate
    some rows and skip others between pages. The primary key appended by
    `ListQuery.build_order_by` is what makes the sequence total.
    """
    repository = SearchableUserRepository()
    marker = secrets.token_hex(4)
    created = [
        await repository.create(db_session, _user(f"page{index}-{marker}"))
        for index in range(5)
    ]
    await db_session.flush()

    query = ListQuery(search=marker, order_by="created_at", order="asc")
    collected = []
    page_sizes = []
    for page in (1, 2, 3):
        items, total = await repository.get_paginated_list(
            db_session, page=page, size=2, query=query
        )
        assert total == 5
        collected.extend(item.id for item in items)
        page_sizes.append(len(items))

    created_at_values = {user.created_at for user in created}
    assert len(created_at_values) == 1
    assert page_sizes == [2, 2, 1]
    assert collected == sorted(user.id for user in created)


async def _two_rows_sharing_a_first_name(
    db_session: AsyncSession,
) -> tuple[str, list[User]]:
    repository = UserRepository()
    first_name = f"twin-{secrets.token_hex(4)}"
    users = [
        await repository.create(
            db_session,
            _user(f"twin{index}-{secrets.token_hex(4)}", first_name=first_name),
        )
        for index in range(2)
    ]
    await db_session.flush()
    return first_name, users


async def _states(db_session: AsyncSession, users: list[User]) -> list[tuple[Any, ...]]:
    rows = await db_session.execute(
        select(User.id, User.last_name)
        .where(User.id.in_([user.id for user in users]))
        .order_by(User.id)
        .execution_options(populate_existing=True)
    )
    return [tuple(row) for row in rows]


async def test_an_ambiguous_update_raises_and_writes_nothing(
    db_session: AsyncSession,
) -> None:
    """With no ORDER BY, taking the first match would write to whichever row the
    planner returned - a filter that names two rows is a caller bug, not a choice."""
    first_name, users = await _two_rows_sharing_a_first_name(db_session)
    before = await _states(db_session, users)

    with pytest.raises(MultipleResultsFound):
        await UserRepository().update(
            db_session, {"last_name": "Changed"}, first_name=first_name
        )
    await db_session.flush()

    assert await _states(db_session, users) == before


async def test_an_ambiguous_delete_raises_and_deletes_nothing(
    db_session: AsyncSession,
) -> None:
    first_name, users = await _two_rows_sharing_a_first_name(db_session)
    before = await _states(db_session, users)

    with pytest.raises(MultipleResultsFound):
        await UserRepository().delete(db_session, first_name=first_name)
    await db_session.flush()

    assert await _states(db_session, users) == before
