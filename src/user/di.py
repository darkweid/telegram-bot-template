from dishka import Provider, Scope, provide

from src.core.telegram.session import BlockedUserRecorder
from src.user.repositories import UserRepository
from src.user.usecases.record_block import RecordBlockUseCase


class UserProvider(Provider):
    scope = Scope.APP

    users = provide(UserRepository)
    record_block = provide(RecordBlockUseCase, provides=BlockedUserRecorder)
