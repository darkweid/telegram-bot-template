import asyncio
from datetime import UTC, datetime
import socket
from typing import Any
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetUpdates, SendMessage, TelegramMethod
from aiogram.types import Update
from dishka import Provider, Scope, provide
from pydantic import SecretStr

from src.main.config import Config
from src.system.schemas import Readiness
from src.system.services import HealthService
from src.user.schemas import TelegramProfile, UserProfile
from src.user.usecases.touch_user import TouchUserUseCase
from tests.fakes.telegram import FakeTelegramSession

WEBHOOK_SECRET = "s" * 32


class PollingSession(FakeTelegramSession):
    """Serves `updates` to the first getUpdates, then answers with empty batches."""

    def __init__(self, *updates: Update) -> None:
        super().__init__()
        self._updates = list(updates)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - signature fixed by BaseSession
    ) -> Any:
        if not isinstance(method, GetUpdates):
            return await super().make_request(bot, method, timeout)
        await self._record(method)
        batch, self._updates = self._updates, []
        if not batch:
            await asyncio.sleep(0.01)  # a long poll that found nothing
        return batch


class Gate:
    """Holds an update inside the user middleware until opened."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.opened = asyncio.Event()
        self.cancelled = False

    async def pass_through(self) -> None:
        self.entered.set()
        try:
            await self.opened.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class RecordingStorage(MemoryStorage):
    """Records how many replies had gone out when the dispatcher closed it."""

    def __init__(self, session: FakeTelegramSession) -> None:
        super().__init__()
        self._session = session
        self.replies_at_close: list[int] = []

    async def close(self) -> None:
        self.replies_at_close.append(len(self._session.sent(SendMessage)))
        await super().close()


class Stubs(Provider):
    """Healthy (or not) dependencies, a gated TouchUserUseCase, a recording storage."""

    def __init__(
        self,
        session: FakeTelegramSession,
        *,
        gate: Gate | None = None,
        readiness: Readiness | None = None,
    ) -> None:
        super().__init__()
        self.gate = gate
        self.readiness = readiness or Readiness(postgres=True, redis=True)
        self.storage = RecordingStorage(session)

    @provide(scope=Scope.REQUEST, override=True)
    def health(self) -> HealthService:
        service = AsyncMock(spec=HealthService)
        service.check_readiness.return_value = self.readiness
        return service

    @provide(scope=Scope.REQUEST, override=True)
    def touch_user(self) -> TouchUserUseCase:
        touch = AsyncMock(spec=TouchUserUseCase)
        touch.execute.side_effect = self._touch
        return touch

    @provide(scope=Scope.APP, override=True)
    def fsm_storage(self) -> BaseStorage:
        return self.storage

    async def _touch(self, profile: TelegramProfile) -> UserProfile:
        if self.gate is not None:
            await self.gate.pass_through()
        return UserProfile(
            id=profile.id,
            first_name=profile.first_name,
            username=profile.username,
            last_name=profile.last_name,
            language_code=profile.language_code,
            locale=None,
            is_reachable=True,
            last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def configured(
    config: Config,
    *,
    shutdown_timeout: float | None = None,
    webhook: bool = False,
) -> Config:
    """`config` on a free local port, optionally in webhook mode or with a
    shorter shutdown timeout. model_copy skips validation; the values are valid."""
    bot = config.bot.model_copy(
        update={
            "BOT_MODE": "webhook" if webhook else "polling",
            "SHUTDOWN_TIMEOUT_SECONDS": (
                shutdown_timeout or config.bot.SHUTDOWN_TIMEOUT_SECONDS
            ),
        }
    )
    web_server = config.web_server.model_copy(
        update={"WEB_SERVER_HOST": "127.0.0.1", "WEB_SERVER_PORT": free_port()}
    )
    hook = config.webhook.model_copy(
        update=(
            {
                "WEBHOOK_BASE_URL": "https://bot.example.com",
                "WEBHOOK_SECRET": SecretStr(WEBHOOK_SECRET),
            }
            if webhook
            else {}
        )
    )
    return config.model_copy(
        update={"bot": bot, "web_server": web_server, "webhook": hook}
    )
