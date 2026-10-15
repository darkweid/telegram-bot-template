from src.core.cache.interface import Cache
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.i18n import SUPPORTED_LOCALES
from src.user.cache import invalidate_user_after_commit
from src.user.exceptions import UnsupportedLocaleError
from src.user.repositories import UserRepository


class SetLocaleUseCase:
    def __init__(
        self, uow: SQLAlchemyUnitOfWork, users: UserRepository, cache: Cache
    ) -> None:
        self._uow = uow
        self._users = users
        self._cache = cache

    async def execute(self, user_id: int, locale: str) -> None:
        if locale not in SUPPORTED_LOCALES:
            raise UnsupportedLocaleError()
        async with self._uow as uow:
            await self._users.set_locale(uow.session, user_id, locale)
            invalidate_user_after_commit(uow, self._cache, user_id)
            await uow.commit()
