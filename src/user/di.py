from dishka import Provider, Scope, provide

from src.core.telegram.session import BlockedUserRecorder
from src.user.repositories import UserRepository
from src.user.usecases.change_membership import ChangeMembershipUseCase
from src.user.usecases.record_block import RecordBlockUseCase
from src.user.usecases.set_locale import SetLocaleUseCase
from src.user.usecases.touch_user import TouchUserUseCase


class UserProvider(Provider):
    scope = Scope.APP

    users = provide(UserRepository)
    record_block = provide(RecordBlockUseCase, provides=BlockedUserRecorder)
    touch_user = provide(TouchUserUseCase, scope=Scope.REQUEST)
    set_locale = provide(SetLocaleUseCase, scope=Scope.REQUEST)
    change_membership = provide(ChangeMembershipUseCase, scope=Scope.REQUEST)
