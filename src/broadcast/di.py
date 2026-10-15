from dishka import Provider, Scope, provide

from src.broadcast.repositories import BroadcastRepository
from src.broadcast.usecases.prepare_broadcast import PrepareBroadcastUseCase
from src.broadcast.usecases.resume_broadcasts import ResumeStalledBroadcastsUseCase
from src.broadcast.usecases.run_broadcast import RunBroadcastUseCase
from src.broadcast.usecases.start_broadcast import StartBroadcastUseCase
from src.broadcast.usecases.stop_broadcast import StopBroadcastUseCase
from src.core.limiter.pacer import Pacer
from src.main.config import BroadcastConfig


class BroadcastProvider(Provider):
    scope = Scope.APP

    broadcasts = provide(BroadcastRepository)
    prepare = provide(PrepareBroadcastUseCase, scope=Scope.REQUEST)
    start = provide(StartBroadcastUseCase, scope=Scope.REQUEST)
    stop = provide(StopBroadcastUseCase, scope=Scope.REQUEST)
    run = provide(RunBroadcastUseCase, scope=Scope.REQUEST)
    resume = provide(ResumeStalledBroadcastsUseCase, scope=Scope.REQUEST)

    @provide(scope=Scope.REQUEST)
    def pacer(self, broadcast: BroadcastConfig) -> Pacer:
        """One per run: a shared pacer would carry one run's schedule into
        the next."""
        return Pacer(broadcast.BROADCAST_RATE_PER_SECOND)
