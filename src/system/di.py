from dishka import Provider, Scope, provide

from src.system.repositories import SystemRepository
from src.system.services import HealthService


class SystemProvider(Provider):
    repository = provide(SystemRepository, scope=Scope.APP)
    health = provide(HealthService, scope=Scope.REQUEST)
