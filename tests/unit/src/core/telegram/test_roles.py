from aiogram.types import User

from src.core.telegram.roles import IsAdmin, IsDeveloper, Roles

ROLES = Roles(admins=frozenset({1, 3}), developers=frozenset({2, 3}))


def _user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Ann")


def test_roles_answer_per_role() -> None:
    """Admin and developer are independent: holding one says nothing about the
    other, and a user holding both is privileged once."""
    assert ROLES.is_admin(1) and not ROLES.is_developer(1)
    assert ROLES.is_developer(2) and not ROLES.is_admin(2)
    assert ROLES.is_privileged(3)
    assert not ROLES.is_privileged(4)


def test_members_lists_each_privileged_id_once_in_order() -> None:
    """The command menu is set per member; a duplicate would set it twice."""
    assert ROLES.members == (1, 2, 3)


async def test_filters_match_their_role_only() -> None:
    """An admin must not pass the developer filter or the other way round,
    or one role would open the other role's commands."""
    assert await IsAdmin()(_user(1), event_from_user=_user(1), roles=ROLES)
    assert not await IsAdmin()(_user(2), event_from_user=_user(2), roles=ROLES)
    assert await IsDeveloper()(_user(2), event_from_user=_user(2), roles=ROLES)
    assert not await IsDeveloper()(_user(4), event_from_user=_user(4), roles=ROLES)


async def test_filters_refuse_an_update_without_a_sender() -> None:
    """A channel post has no sender; it must never pass a role check."""
    assert not await IsAdmin()(_user(1), event_from_user=None, roles=ROLES)
