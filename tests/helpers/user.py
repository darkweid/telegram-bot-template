from datetime import UTC, datetime
from unittest.mock import AsyncMock

from dishka import Provider, Scope, provide

from src.user.schemas import UserProfile
from src.user.usecases.change_membership import ChangeMembershipUseCase
from src.user.usecases.set_locale import SetLocaleUseCase
from src.user.usecases.touch_user import TouchUserUseCase


class UseCases(Provider):
    """The user use cases as mocks; the stored user has `locale` and, with
    `banned`, is banned."""

    def __init__(self, locale: str | None = None, *, banned: bool = False) -> None:
        super().__init__()
        self.touch = AsyncMock(spec=TouchUserUseCase)
        self.touch.execute.side_effect = lambda profile: UserProfile(
            id=profile.id,
            first_name=profile.first_name,
            username=profile.username,
            last_name=profile.last_name,
            language_code=profile.language_code,
            locale=locale,
            is_reachable=True,
            is_banned=banned,
            last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.set_locale = AsyncMock(spec=SetLocaleUseCase)
        self.change = AsyncMock(spec=ChangeMembershipUseCase)

    @provide(scope=Scope.REQUEST, override=True)
    def touch_user(self) -> TouchUserUseCase:
        return self.touch

    @provide(scope=Scope.REQUEST, override=True)
    def set_locale_use_case(self) -> SetLocaleUseCase:
        return self.set_locale

    @provide(scope=Scope.REQUEST, override=True)
    def change_membership(self) -> ChangeMembershipUseCase:
        return self.change
