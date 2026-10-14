"""Worker entrypoint: `taskiq worker src.main.worker:broker --receiver
src.core.tasks.receiver:IdempotencyReceiver`.

Each task gets a REQUEST scope of the worker's container, opened by dishka's
taskiq middleware; the Bot, the engine and Redis live in its APP scope.
"""

from dishka import AsyncContainer, Provider, Scope, from_context
from dishka.integrations.taskiq import TaskiqProvider, setup_dishka
from taskiq import AsyncBroker, TaskiqEvents, TaskiqState

from loggers import setup_logging
from src.main.config import Config, get_settings
from src.main.di import build_container
from src.main.runtime import require_dependencies
from src.main.sentry import init_sentry
from src.main.tasks import build_broker


class WorkerProvider(Provider):
    broker = from_context(provides=AsyncBroker, scope=Scope.APP)


class WorkerLifecycle:
    def __init__(self, config: Config, container: AsyncContainer) -> None:
        self._config = config
        self._container = container

    async def startup(self, _state: TaskiqState) -> None:
        setup_logging()
        # Inside the loop: Sentry's asyncio integration patches the running
        # loop's task factory.
        init_sentry(self._config)
        # A worker without its database would take every task and fail it.
        await require_dependencies(self._container)

    async def shutdown(self, _state: TaskiqState) -> None:
        # The bot session, Redis and the engine close here.
        await self._container.close()


def create_worker(config: Config, *providers: Provider) -> AsyncBroker:
    """The worker's broker with its container attached. `providers` override
    production ones in tests."""
    broker = build_broker(config)
    container = build_container(
        config,
        TaskiqProvider(),
        WorkerProvider(),
        *providers,
        context={AsyncBroker: broker},
    )
    setup_dishka(container, broker)
    lifecycle = WorkerLifecycle(config, container)
    broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, lifecycle.startup)
    broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, lifecycle.shutdown)
    return broker


broker = create_worker(get_settings())
