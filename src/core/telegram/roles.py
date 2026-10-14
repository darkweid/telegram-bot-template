from dataclasses import dataclass

from aiogram.filters import Filter
from aiogram.types import TelegramObject, User


@dataclass(frozen=True, slots=True)
class Roles:
    """Who holds which role. Built by the composition root from the config,
    so that core code checks roles without reading settings."""

    admins: frozenset[int]
    developers: frozenset[int]

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def is_developer(self, user_id: int) -> bool:
        return user_id in self.developers

    def is_privileged(self, user_id: int) -> bool:
        return self.is_admin(user_id) or self.is_developer(user_id)

    @property
    def members(self) -> tuple[int, ...]:
        return tuple(sorted(self.admins | self.developers))


class IsAdmin(Filter):
    """`roles` comes from the dispatcher's workflow data (create_dispatcher)."""

    async def __call__(
        self, event: TelegramObject, event_from_user: User | None, roles: Roles
    ) -> bool:
        return event_from_user is not None and roles.is_admin(event_from_user.id)


class IsDeveloper(Filter):
    async def __call__(
        self, event: TelegramObject, event_from_user: User | None, roles: Roles
    ) -> bool:
        return event_from_user is not None and roles.is_developer(event_from_user.id)
