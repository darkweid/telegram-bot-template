import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from src.main import __main__ as entry
from src.main.config import Config


@pytest.mark.parametrize("mode", ["polling", "webhook"])
async def test_sentry_is_initialised_inside_the_running_loop(
    settings: Config, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Sentry's asyncio integration patches the running loop; initialised
    # before asyncio.run it patches nothing and update tasks go unreported.
    loops: list[asyncio.AbstractEventLoop] = []
    served: list[str] = []

    def init_sentry(config: Config) -> bool:
        loops.append(asyncio.get_running_loop())
        return False

    @asynccontextmanager
    async def bot_runtime(config: Config) -> Any:
        async def me() -> Any:
            return SimpleNamespace(username="test_bot")

        yield SimpleNamespace(bot=SimpleNamespace(me=me))

    async def serve(runtime: Any) -> None:
        served.append(mode)

    config = settings.model_copy(
        update={"bot": settings.bot.model_copy(update={"BOT_MODE": mode})}
    )
    monkeypatch.setattr(entry, "init_sentry", init_sentry)
    monkeypatch.setattr(entry, "bot_runtime", bot_runtime)
    monkeypatch.setattr(entry, "poll", serve)
    monkeypatch.setattr(entry, "serve_webhook", serve)

    await entry.run(config)

    assert loops == [asyncio.get_running_loop()]
    assert served == [mode]
