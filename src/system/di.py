from dishka import Provider, Scope, provide

from src.core.outbox.repositories import OutboxRepository
from src.system.repositories import SystemRepository
from src.system.services import HealthService


class SystemProvider(Provider):
    repository = provide(SystemRepository, scope=Scope.APP)
    outbox = provide(OutboxRepository, scope=Scope.APP)
    health = provide(HealthService, scope=Scope.REQUEST)
