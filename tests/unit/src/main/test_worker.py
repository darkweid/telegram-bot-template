from aiogram.utils.i18n import I18n
from dishka import FromDishka
from dishka.integrations.taskiq import inject
import pytest

from src.main import worker as worker_module
from src.main.config import Config
from src.main.runtime import DependencyUnavailableError
from src.main.worker import create_worker
from src.system.schemas import Readiness
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.providers import TestOverrides
from tests.helpers.runtime import Stubs


async def echo_version(config: FromDishka[Config]) -> str:
    return config.app.VERSION


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
