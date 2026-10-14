from src.core.cache.interface import Cache
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.schemas import Actor
from src.core.utils.datetime_utils import get_utc_now
from src.user.cache import invalidate_user_after_commit
from src.user.events import UserRegistered
from src.user.repositories import UserRepository
from src.user.schemas import TelegramProfile


class ChangeMembershipUseCase:
    """Follow the bot's membership in a user's private chat.

    Telegram reports "kicked" when the user blocks the bot and "member" when
    they unblock or restart it - the only way to learn that the bot became
    unreachable without trying to send something.

    A "member" can be the first update Telegram sends about a user, before
    any message: a user stored here for the first time publishes
    `UserRegistered` like one stored by `TouchUserUseCase`.
    """

    def __init__(
        self, uow: SQLAlchemyUnitOfWork, users: UserRepository, cache: Cache
    ) -> None:
        self._uow = uow
        self._users = users
        self._cache = cache

    async def execute(self, profile: TelegramProfile, *, is_member: bool) -> None:
        now = get_utc_now()
        async with self._uow as uow:
            if is_member:
                upserted = await self._users.upsert_from_telegram(
                    uow.session, profile, now
                )
                if upserted.created:
                    await uow.publish(
                        Actor.telegram_user(profile.id),
                        UserRegistered(user_id=profile.id),
                    )
            else:
                await self._users.set_reachable(uow.session, profile.id, False, now)
            invalidate_user_after_commit(uow, self._cache, profile.id)
            await uow.commit()
