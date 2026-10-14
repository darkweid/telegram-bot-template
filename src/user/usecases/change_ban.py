from dataclasses import replace

from loggers import get_logger
from src.core.cache.interface import Cache
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.telegram.roles import Roles
from src.core.utils.datetime_utils import get_utc_now
from src.user.cache import invalidate_user_after_commit
from src.user.exceptions import (
    AmbiguousUsernameError,
    ProtectedUserError,
    SelfBanError,
    UserNotFoundError,
)
from src.user.models import User
from src.user.repositories import UserRepository
from src.user.schemas import BanChange, UserProfile
from src.user.services import UserReference

logger = get_logger(__name__)


class ChangeBanUseCase:
    """Ban or unban a user on an admin's command.

    Admins and developers cannot be banned, and nobody bans themselves; an
    unban has no such limits, so a stale ban on someone who later got a role
    can be lifted.
    """

    def __init__(
        self,
        uow: SQLAlchemyUnitOfWork,
        users: UserRepository,
        cache: Cache,
        roles: Roles,
    ) -> None:
        self._uow = uow
        self._users = users
        self._cache = cache
        self._roles = roles

    async def execute(
        self, admin_id: int, reference: UserReference, *, banned: bool
    ) -> BanChange:
        async with self._uow as uow:
            target = UserProfile.from_model(await self._find(uow, reference))
            if banned and target.id == admin_id:
                raise SelfBanError()
            if banned and self._roles.is_privileged(target.id):
                raise ProtectedUserError()
            # The conditional write decides, not the snapshot read above:
            # another admin may have changed the ban in between.
            changed = await self._users.set_banned(
                uow.session, target.id, banned=banned, by=admin_id, now=get_utc_now()
            )
            if not changed:
                return BanChange(user=replace(target, is_banned=banned), changed=False)
            invalidate_user_after_commit(uow, self._cache, target.id)
            await uow.commit()
        logger.info(
            "User %s %s by %s", target.id, "banned" if banned else "unbanned", admin_id
        )
        return BanChange(user=replace(target, is_banned=banned), changed=True)

    async def _find(self, uow: SQLAlchemyUnitOfWork, reference: UserReference) -> User:
        if isinstance(reference, int):
            found = await self._users.get_single(uow.session, id=reference)
            if found is None:
                raise UserNotFoundError()
            return found
        matches = await self._users.find_by_username(uow.session, reference)
        if not matches:
            raise UserNotFoundError()
        if len(matches) > 1:
            raise AmbiguousUsernameError(username=reference)
        return matches[0]
