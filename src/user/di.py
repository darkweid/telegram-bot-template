from dishka import Provider, Scope, provide

from src.core.telegram.session import UnreachableUserRecorder
from src.user.repositories import UserRepository
from src.user.usecases.change_ban import ChangeBanUseCase
from src.user.usecases.change_membership import ChangeMembershipUseCase
from src.user.usecases.get_user_stats import GetUserStatsUseCase
from src.user.usecases.record_unreachable import RecordUnreachableUseCase
from src.user.usecases.set_locale import SetLocaleUseCase
from src.user.usecases.touch_user import TouchUserUseCase


class UserProvider(Provider):
    scope = Scope.APP

    users = provide(UserRepository)
    record_unreachable = provide(
        RecordUnreachableUseCase, provides=UnreachableUserRecorder
    )
    touch_user = provide(TouchUserUseCase, scope=Scope.REQUEST)
    set_locale = provide(SetLocaleUseCase, scope=Scope.REQUEST)
    change_membership = provide(ChangeMembershipUseCase, scope=Scope.REQUEST)
    change_ban = provide(ChangeBanUseCase, scope=Scope.REQUEST)
    get_user_stats = provide(GetUserStatsUseCase, scope=Scope.REQUEST)
