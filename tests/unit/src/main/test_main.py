import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from src.main import __main__ as entry
from src.main.config import Config
from src.main.webhook import WebhookRegistrationError
from tests.helpers.logs import captured_logs


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


def test_a_startup_error_is_one_critical_line_and_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup failures caused by the environment read the same way: the
    reason on one line, exit code 1, so the orchestrator's restart loop does
    not bury the cause under tracebacks."""

    async def run(config: Config) -> None:
        raise WebhookRegistrationError("Telegram refused the webhook: x")

    monkeypatch.setattr(entry, "run", run)
    monkeypatch.setattr(entry, "setup_logging", lambda: None)
    with (
        captured_logs("src.main.__main__") as records,
        pytest.raises(SystemExit) as exit_,
    ):
        entry.main()
    assert exit_.value.code == 1
    assert [(r.levelname, r.getMessage(), r.exc_info) for r in records] == [
        ("CRITICAL", "Cannot start: Telegram refused the webhook: x", None)
    ]
