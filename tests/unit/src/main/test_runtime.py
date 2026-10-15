import asyncio
import logging
import os
import signal

from aiogram.methods import SetMyCommands
from aiogram.utils.i18n import I18n
import pytest

from src.core.i18n import SUPPORTED_LOCALES
from src.main import runtime as runtime_module
from src.main.config import Config
from src.main.runtime import (
    DependencyUnavailableError,
    bot_runtime,
    finish_updates,
    wait_for_stop_signal,
)
from src.system.schemas import Readiness
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.logs import captured_logs
from tests.helpers.providers import TestOverrides
from tests.helpers.runtime import Stubs


async def test_runtime_publishes_the_menu(settings: Config, i18n: I18n) -> None:
    session = FakeTelegramSession()
    overrides = TestOverrides(session, i18n)
    async with bot_runtime(settings, overrides, Stubs(session)) as runtime:
        assert runtime.bot.session is session
    assert len(session.sent(SetMyCommands)) == 1 + len(SUPPORTED_LOCALES)


@pytest.mark.parametrize(
    ("readiness", "message"),
    [
        (Readiness(postgres=False, redis=True), "PostgreSQL unreachable"),
        (Readiness(postgres=True, redis=False), "Redis unreachable"),
        (Readiness(postgres=False, redis=False), "PostgreSQL and Redis unreachable"),
    ],
)
async def test_runtime_refuses_to_start_without_its_dependencies(
    settings: Config, i18n: I18n, readiness: Readiness, message: str
) -> None:
    session = FakeTelegramSession()
    stubs = Stubs(session, readiness=readiness)
    with pytest.raises(DependencyUnavailableError, match=f"^{message}$"):
        async with bot_runtime(settings, TestOverrides(session, i18n), stubs):
            pytest.fail("the runtime must not start")
    assert session.requests == []  # nothing was sent to Telegram


async def test_finish_updates_waits_for_quick_ones() -> None:
    done: list[int] = []

    async def quick(n: int) -> None:
        await asyncio.sleep(0.01)
        done.append(n)

    tasks = {asyncio.create_task(quick(n)) for n in range(3)}
    await finish_updates(tasks, grace_seconds=1)
    assert sorted(done) == [0, 1, 2]


async def test_finish_updates_cancels_what_outlives_the_timeout() -> None:
    stuck = asyncio.create_task(asyncio.sleep(3600))
    with captured_logs("src.main.runtime") as records:
        async with asyncio.timeout(1):
            await finish_updates({stuck}, grace_seconds=0.05)
    assert stuck.cancelled()
    assert any(
        r.levelno == logging.WARNING and "Cancelling 1 update" in r.getMessage()
        for r in records
    )


async def test_finish_updates_with_nothing_in_progress_returns_at_once() -> None:
    async with asyncio.timeout(0.1):
        await finish_updates(set(), grace_seconds=3600)


async def test_wait_for_stop_signal_returns_on_sigterm() -> None:
    waiter = asyncio.create_task(wait_for_stop_signal())
    # One loop step runs the task up to its first await, after the handlers
    # are installed: SIGTERM below can only reach the handler, never pytest.
    await asyncio.sleep(0)
    os.kill(os.getpid(), signal.SIGTERM)
    with captured_logs("src.main.runtime") as records:
        async with asyncio.timeout(1):
            await waiter
    assert [r.getMessage() for r in records] == ["Received SIGTERM, stopping"]


async def test_http_clients_close_with_the_runtime(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session left open warns "Unclosed client session" at exit and leaks
    the connector's sockets. The clients close before the container, while
    the loop that opened their sessions still runs."""
    events: list[str] = []

    async def close_http_clients() -> None:
        events.append("http clients")

    async def close_bot_session() -> None:
        events.append("bot session")

    monkeypatch.setattr(runtime_module, "close_http_clients", close_http_clients)
    session = FakeTelegramSession()
    monkeypatch.setattr(session, "close", close_bot_session)
    async with bot_runtime(settings, TestOverrides(session, i18n), Stubs(session)):
        assert events == []
    assert events == ["http clients", "bot session"]
