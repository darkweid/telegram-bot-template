from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock

from aiogram.utils.i18n import I18n
from aiohttp.test_utils import TestClient, TestServer
from dishka import AsyncContainer, Provider, Scope, provide
import pytest

from src.main.config import Config
from src.main.health import create_web_app
from src.system.schemas import HealthReport, Readiness
from src.system.services import HealthService
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.providers import build_test_container

ClientFor = Callable[[Readiness], Awaitable[TestClient]]


class StubHealth(Provider):
    def __init__(self, readiness: Readiness) -> None:
        super().__init__()
        self.service = AsyncMock(spec=HealthService)
        self.service.check_readiness.return_value = readiness
        self.service.get_report.return_value = HealthReport(
            status="ok" if readiness.ok else "degraded",
            version="1.2.3",
            mode="polling",
            postgres=readiness.postgres,
            redis=readiness.redis,
            redis_memory_used_ratio=None,
        )

    @provide(scope=Scope.REQUEST, override=True)
    def health(self) -> HealthService:
        return self.service


@pytest.fixture
async def client_for(settings: Config, i18n: I18n) -> AsyncIterator[ClientFor]:
    clients: list[TestClient] = []
    containers: list[AsyncContainer] = []

    async def make(readiness: Readiness) -> TestClient:
        container = build_test_container(
            settings, FakeTelegramSession(), i18n, StubHealth(readiness)
        )
        containers.append(container)
        client = TestClient(TestServer(create_web_app(container)))
        await client.start_server()
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()
    for container in containers:
        await container.close()


async def test_live_needs_nothing(client_for: ClientFor) -> None:
    client = await client_for(Readiness(postgres=False, redis=False))
    response = await client.get("/live")
    assert response.status == 200
    assert await response.json() == {"status": "ok"}


async def test_ready_is_200_when_everything_answers(client_for: ClientFor) -> None:
    client = await client_for(Readiness(postgres=True, redis=True))
    response = await client.get("/ready")
    assert response.status == 200
    assert await response.json() == {"status": "ok", "postgres": True, "redis": True}


@pytest.mark.parametrize(("postgres", "redis"), [(False, True), (True, False)])
async def test_ready_is_503_when_a_dependency_is_down(
    client_for: ClientFor, postgres: bool, redis: bool
) -> None:
    client = await client_for(Readiness(postgres=postgres, redis=redis))
    response = await client.get("/ready")
    assert response.status == 503
    assert await response.json() == {
        "status": "unavailable",
        "postgres": postgres,
        "redis": redis,
    }


async def test_health_reports_details_with_200_even_when_degraded(
    client_for: ClientFor,
) -> None:
    client = await client_for(Readiness(postgres=False, redis=True))
    response = await client.get("/health")
    assert response.status == 200
    assert await response.json() == {
        "status": "degraded",
        "version": "1.2.3",
        "mode": "polling",
        "postgres": False,
        "redis": True,
        "redis_memory_used_ratio": None,
    }


async def test_probes_answer_head(client_for: ClientFor) -> None:
    client = await client_for(Readiness(postgres=True, redis=True))
    for path in ("/live", "/ready", "/health"):
        assert (await client.head(path)).status == 200
