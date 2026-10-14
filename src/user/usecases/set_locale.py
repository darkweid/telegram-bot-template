from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.i18n import SUPPORTED_LOCALES
from src.user.exceptions import UnsupportedLocaleError
from src.user.repositories import UserRepository


class SetLocaleUseCase:
    def __init__(self, uow: SQLAlchemyUnitOfWork, users: UserRepository) -> None:
        self._uow = uow
        self._users = users

    async def execute(self, user_id: int, locale: str) -> None:
        if locale not in SUPPORTED_LOCALES:
            raise UnsupportedLocaleError()
        async with self._uow as uow:
            await self._users.set_locale(uow.session, user_id, locale)
            await uow.commit()
