import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiogram.utils.i18n import I18n
from dishka import AsyncContainer, Provider

from src.core.telegram.background import BackgroundRunner
from src.main.config import Config
from src.main.dispatcher import create_dispatcher
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.providers import build_test_container


@dataclass
class RunningBot:
    dp: Dispatcher
    bot: Bot
    session: FakeTelegramSession
    container: AsyncContainer

    async def feed(self, update: Update) -> None:
        await self.dp.feed_update(self.bot, update)

    async def settle(self) -> None:
        """Wait for the background work the updates started, and the work it
        started in turn; fails after 2 seconds, so work that never ends
        cannot hang the suite."""
        runner = await self.container.get(BackgroundRunner)
        async with asyncio.timeout(2):
            while runner.tasks:
                await asyncio.gather(*runner.tasks, return_exceptions=True)


@asynccontextmanager
async def running_bot(
    config: Config,
    i18n: I18n,
    *providers: Provider,
    alert_session: FakeTelegramSession | None = None,
) -> AsyncIterator[RunningBot]:
    """The production dispatcher on a fake network, started and stopped the
    way polling and webhook start and stop it. Developer alerts leave through
    `alert_session`, never through `RunningBot.session`."""
    session = FakeTelegramSession()
    container = build_test_container(
        config, session, i18n, *providers, alert_session=alert_session
    )
    try:
        dp = await create_dispatcher(container)
        bot = await container.get(Bot)
        # Startup is where dishka wraps the FromDishka handlers (inject_router).
        await dp.emit_startup(bot=bot)
        try:
            yield RunningBot(dp=dp, bot=bot, session=session, container=container)
        finally:
            await dp.emit_shutdown(bot=bot)
    finally:
        await container.close()
