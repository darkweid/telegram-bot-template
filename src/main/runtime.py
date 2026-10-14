"""What polling and webhook share: the container, the startup check, the
dispatcher and the menu, and the rules for stopping."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import signal
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.utils.i18n import I18n
from aiohttp import web
from dishka import AsyncContainer, Provider

from loggers import get_logger
from src.core.http.client import close_http_clients
from src.core.telegram.roles import Roles
from src.main.commands import set_bot_commands
from src.main.config import Config, WebServerConfig
from src.main.di import build_container
from src.main.dispatcher import create_dispatcher
from src.system.services import HealthService

logger = get_logger(__name__)


class StartupError(RuntimeError):
    """The environment keeps the bot from starting; the message says why."""


class DependencyUnavailableError(StartupError):
    """PostgreSQL or Redis did not answer at startup."""


@dataclass(frozen=True, slots=True)
class BotRuntime:
    config: Config
    container: AsyncContainer
    dp: Dispatcher
    bot: Bot


@asynccontextmanager
async def bot_runtime(
    config: Config, *providers: Provider
) -> AsyncIterator[BotRuntime]:
    """Everything a running bot needs; released when the block exits.

    Fails fast: a bot started without its database or Redis would accept
    updates and fail every one of them, so the process exits instead and the
    orchestrator restarts it. `providers` override production ones in tests.
    """
    container = build_container(config, *providers)
    try:
        await _require_dependencies(container)
        dp = await create_dispatcher(container)
        bot = await container.get(Bot)
        await set_bot_commands(
            bot, await container.get(I18n), await container.get(Roles)
        )
        yield BotRuntime(config=config, container=container, dp=dp, bot=bot)
    finally:
        # Sessions opened on this loop close before the loop stops; the
        # container then closes the bot session, FSM storage, Redis and the engine.
        await close_http_clients()
        await container.close()


async def _require_dependencies(container: AsyncContainer) -> None:
    async with container() as scope:
        readiness = await (await scope.get(HealthService)).check_readiness()
    missing = [
        name
        for name, available in (
            ("PostgreSQL", readiness.postgres),
            ("Redis", readiness.redis),
        )
        if not available
    ]
    if missing:
        raise DependencyUnavailableError(f"{' and '.join(missing)} unreachable")


async def finish_updates(tasks: set[asyncio.Task[Any]], grace_seconds: float) -> None:
    """Give the updates still being handled `grace_seconds`, then cancel the rest.

    Telegram already counts them as delivered, so an update cut short is lost;
    the grace period only bounds how long a stuck one can hold up a deploy.
    """
    pending = set(tasks)
    if not pending:
        return
    logger.info(
        "Waiting up to %ss for %d update(s) in progress",
        grace_seconds,
        len(pending),
    )
    _, pending = await asyncio.wait(pending, timeout=grace_seconds)
    if not pending:
        return
    logger.warning(
        "Cancelling %d update(s) still running after %ss",
        len(pending),
        grace_seconds,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@asynccontextmanager
async def serving(
    app: web.Application, web_server: WebServerConfig
) -> AsyncIterator[None]:
    """Serve `app` for the duration of the block; on exit aiohttp runs the
    app's shutdown callbacks."""
    # No access log: the probes are polled every few seconds and would drown
    # everything else.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(
            runner, web_server.WEB_SERVER_HOST, web_server.WEB_SERVER_PORT
        )
        await site.start()
        logger.info(
            "Serving HTTP on %s:%s",
            web_server.WEB_SERVER_HOST,
            web_server.WEB_SERVER_PORT,
        )
        yield
    finally:
        await runner.cleanup()


async def wait_for_stop_signal() -> None:
    """Return on the first SIGTERM (docker stop) or SIGINT (Ctrl+C)."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    signals = (signal.SIGTERM, signal.SIGINT)

    def on_signal(sig: signal.Signals) -> None:
        logger.info("Received %s, stopping", sig.name)
        stop.set()

    for sig in signals:
        loop.add_signal_handler(sig, on_signal, sig)
    try:
        await stop.wait()
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
