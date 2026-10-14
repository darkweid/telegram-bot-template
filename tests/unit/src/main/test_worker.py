from uuid import uuid4

from aiogram.utils.i18n import I18n
from dishka import FromDishka
from dishka.integrations.taskiq import inject
import pytest
from taskiq import InMemoryBroker

from src.core.outbox.interface import TaskPublisher
from src.core.outbox.publisher import UnregisteredTaskError
from src.core.tasks.middlewares import FAILURE_REPORTER_STATE
from src.core.telegram.alerts import DeveloperAlerter
from src.main import worker as worker_module
from src.main.config import Config
from src.main.runtime import DependencyUnavailableError
from src.main.worker import create_worker
from src.system.schemas import Readiness
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.providers import SessionOnly, TestOverrides
from tests.helpers.runtime import Stubs


async def echo_version(config: FromDishka[Config]) -> str:
    return config.app.VERSION


async def publish_unregistered(publisher: FromDishka[TaskPublisher]) -> str:
    # Unregistered, so nothing runs inline and nothing is marked in the
    # database; a start would come before the refusal.
    try:
        await publisher.publish(uuid4(), "test.missing", [], {})
    except UnregisteredTaskError:
        return "refused"
    return "sent"


@pytest.fixture(autouse=True)
def _keep_root_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker configures the root logger on startup; tests share one process.
    monkeypatch.setattr(worker_module, "setup_logging", lambda: None)


async def test_a_task_runs_in_a_request_scope_of_the_worker_container(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    broker = create_worker(settings, TestOverrides(session, i18n), Stubs(session))
    broker.register_task(inject(echo_version, patch_module=True), task_name="test.echo")
    await broker.startup()
    try:
        task = await broker.find_task("test.echo").kiq()
        result = await task.wait_result()
    finally:
        await broker.shutdown()
    assert result.return_value == settings.app.VERSION


async def test_a_worker_without_its_database_does_not_start(
    settings: Config, i18n: I18n
) -> None:
    """A worker that started anyway would take every task and fail it."""
    session = FakeTelegramSession()
    broker = create_worker(
        settings,
        TestOverrides(session, i18n),
        Stubs(session, readiness=Readiness(postgres=False, redis=True)),
    )
    try:
        with pytest.raises(DependencyUnavailableError, match="PostgreSQL"):
            await broker.startup()
    finally:
        await broker.shutdown()


async def test_the_workers_publisher_never_starts_the_clis_broker(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The taskiq CLI starts the worker's broker; a second start would rerun
    the worker's startup hooks. TestOverrides would replace the publisher, so
    the production one is resolved here."""
    starts: list[InMemoryBroker] = []
    original = InMemoryBroker.startup

    async def counting_startup(self: InMemoryBroker) -> None:
        starts.append(self)
        await original(self)

    monkeypatch.setattr(InMemoryBroker, "startup", counting_startup)
    session = FakeTelegramSession()
    broker = create_worker(settings, SessionOnly(session, i18n), Stubs(session))
    broker.register_task(
        inject(publish_unregistered, patch_module=True), task_name="test.publish"
    )
    await broker.startup()
    try:
        task = await broker.find_task("test.publish").kiq()
        result = await task.wait_result()
    finally:
        await broker.shutdown()
    assert result.return_value == "refused"
    assert starts == [broker]


async def test_a_started_worker_alerts_through_its_alerter(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    broker = create_worker(settings, TestOverrides(session, i18n), Stubs(session))
    await broker.startup()
    try:
        reporter = broker.state[FAILURE_REPORTER_STATE]
    finally:
        await broker.shutdown()
    assert isinstance(reporter, DeveloperAlerter)


async def test_the_worker_closes_http_clients_on_shutdown(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client's session belongs to the worker's loop and must close on it."""
    calls: list[str] = []

    async def close_http_clients() -> None:
        calls.append("http")

    monkeypatch.setattr(worker_module, "close_http_clients", close_http_clients)
    session = FakeTelegramSession()
    broker = create_worker(settings, TestOverrides(session, i18n), Stubs(session))
    await broker.startup()
    await broker.shutdown()
    assert calls == ["http"]
