import asyncio
import logging
from unittest.mock import MagicMock

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.utils.i18n import I18n, get_i18n
from dishka import AsyncContainer
import pytest
import sentry_sdk

from src.core.errors.exceptions import CoreException
from src.core.telegram import session as session_module
from src.core.telegram.alerts import DeveloperAlerter
from src.core.telegram.background import BackgroundRunner
from src.core.telegram.session import MAX_RETRY_SLEEP_SECONDS, update_sleep_budget
from src.core.update_context import (
    UpdateContext,
    bind_update_context,
    get_update_context,
)
from src.main.config import Config
from tests.helpers.dispatcher import running_bot
from tests.helpers.logs import captured_logs

LOGGER = "src.core.telegram.background"
CONTEXT = UpdateContext(update_id=7, update_type="message", user_id=42, chat_id=42)


class Expected(CoreException):
    log_level = "info"


class Reported(CoreException):
    log_level = "error"
    capture_to_sentry = True


@pytest.fixture
def sentry(monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    captured: list[BaseException] = []

    def capture(error: BaseException) -> str:
        captured.append(error)
        return "event-1"

    monkeypatch.setattr(sentry_sdk, "capture_exception", capture)
    return captured


@pytest.fixture
def alerter() -> MagicMock:
    return MagicMock(spec=DeveloperAlerter)


@pytest.fixture
def runner(alerter: MagicMock) -> BackgroundRunner:
    return BackgroundRunner(MagicMock(spec=AsyncContainer), alerter)


async def _settle(runner: BackgroundRunner) -> None:
    async with asyncio.timeout(2):
        await asyncio.gather(*runner.tasks, return_exceptions=True)


async def _raise(error: BaseException) -> None:
    raise error


async def _wait_forever(started: asyncio.Event) -> None:
    started.set()
    await asyncio.Event().wait()


async def test_the_work_sees_the_updates_context_and_locale(
    runner: BackgroundRunner, i18n: I18n
) -> None:
    """Its log lines carry the update's ids and its texts the user's
    language, though the update itself is long gone."""
    seen: list[tuple[UpdateContext | None, str, I18n]] = []

    async def work(_: AsyncContainer) -> None:
        seen.append((get_update_context(), i18n.current_locale, get_i18n()))

    with bind_update_context(CONTEXT), i18n.context(), i18n.use_locale("ru"):
        runner.start("probe", work)
    await _settle(runner)
    assert seen == [(CONTEXT, "ru", i18n)]


async def test_the_work_gets_the_root_container(settings: Config, i18n: I18n) -> None:
    """The update's scope closes with the update; the work opens scopes of
    its own on a container that outlives it."""
    received: list[AsyncContainer] = []
    resolved: list[I18n] = []

    async def work(container: AsyncContainer) -> None:
        received.append(container)
        async with container() as scope:
            resolved.append(await scope.get(I18n))

    async with running_bot(settings, i18n) as running:
        async with running.container() as request:
            runner = await request.get(BackgroundRunner)
            runner.start("probe", work)
        await _settle(runner)
    assert received == [running.container]
    assert resolved == [i18n]


async def test_the_work_has_a_flood_budget_of_its_own(
    runner: BackgroundRunner,
) -> None:
    """The update's sleep budget may be spent by the time the work calls
    Telegram, and the work's own sleeps must not eat into a live update's."""
    budgets: list[float] = []

    async def work(_: AsyncContainer) -> None:
        budget = session_module._update_sleep_budget.get()
        assert budget is not None and budget is not spent
        budgets.append(budget.remaining)

    with update_sleep_budget():
        spent = session_module._update_sleep_budget.get()
        assert spent is not None
        spent.remaining = 0
        runner.start("probe", work)
        await _settle(runner)
    assert budgets == [MAX_RETRY_SLEEP_SECONDS]


async def test_a_core_exception_is_logged_at_its_level_and_not_alerted(
    runner: BackgroundRunner, alerter: MagicMock, sentry: list[BaseException]
) -> None:
    """An expected outcome (a refused model, no notes) reads in the log the
    way it would from a handler and wakes nobody."""
    with captured_logs(LOGGER) as records:
        runner.start("probe", lambda _: _raise(Expected("detail 7")))
        await _settle(runner)
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, "Expected: detail 7")
    ]
    assert sentry == []
    alerter.alert_error_soon.assert_not_called()


async def test_a_core_exception_that_asks_for_sentry_reaches_it(
    runner: BackgroundRunner, alerter: MagicMock, sentry: list[BaseException]
) -> None:
    error = Reported()
    runner.start("probe", lambda _: _raise(error))
    await _settle(runner)
    assert sentry == [error]
    alerter.alert_error_soon.assert_not_called()


async def test_an_unexpected_error_is_logged_reported_and_alerted_with_its_name(
    runner: BackgroundRunner, alerter: MagicMock, sentry: list[BaseException]
) -> None:
    """A bug in background work has no handler to land in: the runner is the
    only place that sees it, so it must report it the way an update would."""
    error = ValueError("boom")
    with captured_logs(LOGGER) as records, bind_update_context(CONTEXT):
        runner.start("note.summarize", lambda _: _raise(error))
        await _settle(runner)
    [record] = records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Background work note.summarize failed: ValueError"
    assert sentry == [error]
    [call] = alerter.alert_error_soon.call_args_list
    alert = call.args[0]
    assert alert.error is error
    assert alert.origin == "background note.summarize"
    assert alert.user_id == 42
    assert alert.sentry_event_id == "event-1"


async def test_a_forbidden_error_is_logged_at_info_only(
    runner: BackgroundRunner, alerter: MagicMock, sentry: list[BaseException]
) -> None:
    """The user blocked the bot while the work ran: nobody to tell and
    nothing to fix."""
    error = TelegramForbiddenError(
        SendMessage(chat_id=42, text="x"), "Forbidden: bot was blocked by the user"
    )
    with captured_logs(LOGGER) as records:
        runner.start("probe", lambda _: _raise(error))
        await _settle(runner)
    assert [r.levelno for r in records] == [logging.INFO]
    assert sentry == []
    alerter.alert_error_soon.assert_not_called()


async def test_a_cancelled_work_is_not_reported(
    runner: BackgroundRunner, alerter: MagicMock, sentry: list[BaseException]
) -> None:
    """A stopping bot cancels what is left: not a failure to investigate."""
    started = asyncio.Event()
    with captured_logs(LOGGER) as records:
        runner.start("probe", lambda _: _wait_forever(started))
        await started.wait()
        await runner.drain(0)
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, "Cancelling 1 background task(s) still running after 0s"),
        (logging.INFO, "Background work probe cancelled"),
    ]
    assert sentry == []
    alerter.alert_error_soon.assert_not_called()


async def test_drain_waits_for_work_that_finishes_in_time(
    runner: BackgroundRunner,
) -> None:
    release = asyncio.Event()
    finished: list[str] = []

    async def work(_: AsyncContainer) -> None:
        await release.wait()
        finished.append("done")

    runner.start("probe", work)
    draining = asyncio.create_task(runner.drain(2))
    await asyncio.sleep(0.05)
    assert not draining.done()
    release.set()
    async with asyncio.timeout(2):
        await draining
    assert finished == ["done"]


async def test_drain_cancels_work_past_the_timeout(runner: BackgroundRunner) -> None:
    """The deploy waits only so long; past it the work is cancelled, and
    drain returns once the cancelled work has finished its own cleanup."""
    started = asyncio.Event()
    cleaned: list[str] = []

    async def work(_: AsyncContainer) -> None:
        try:
            await _wait_forever(started)
        finally:
            await asyncio.sleep(0.01)
            cleaned.append("cleaned")

    runner.start("probe", work)
    await started.wait()
    with captured_logs(LOGGER) as records:
        async with asyncio.timeout(2):
            await runner.drain(0.05)
    assert cleaned == ["cleaned"]
    assert runner.tasks == frozenset()
    assert any(
        r.levelno == logging.WARNING
        and r.getMessage()
        == "Cancelling 1 background task(s) still running after 0.05s"
        for r in records
    )


async def test_starting_after_drain_is_refused(runner: BackgroundRunner) -> None:
    """A stopping bot takes no updates, so a start then is a bug, and work
    started then would be cut off unseen."""
    await runner.drain(1)
    with pytest.raises(RuntimeError, match="probe"):
        runner.start("probe", lambda _: _raise(ValueError()))
    assert runner.tasks == frozenset()


async def test_closing_the_container_cancels_running_work(
    settings: Config, i18n: I18n
) -> None:
    """Tests and a failed start close the container without a drain; no
    task may outlive it."""
    started = asyncio.Event()
    async with running_bot(settings, i18n) as running:
        runner = await running.container.get(BackgroundRunner)
        runner.start("probe", lambda _: _wait_forever(started))
        await started.wait()
        [task] = runner.tasks
    assert task.cancelled()
    assert runner.tasks == frozenset()


async def test_finished_work_leaves_the_set(runner: BackgroundRunner) -> None:
    """A finished task kept in the set would grow it for the life of the bot."""

    async def work(_: AsyncContainer) -> None:
        return None

    runner.start("probe", work)
    assert len(runner.tasks) == 1
    await _settle(runner)
    await asyncio.sleep(0)
    assert runner.tasks == frozenset()
