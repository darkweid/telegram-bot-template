import asyncio
import logging
from unittest.mock import AsyncMock

from aiogram.methods import DeleteWebhook, GetUpdates, SendMessage
from aiogram.utils.i18n import I18n
import aiohttp

from src.main.config import Config
from src.main.polling import poll
from src.main.runtime import BotRuntime, bot_runtime
from tests.factories.updates import build_message_update
from tests.helpers.logs import captured_logs
from tests.helpers.providers import TestOverrides
from tests.helpers.runtime import Gate, PollingSession, Stubs, configured


async def _until(condition: asyncio.Event) -> None:
    async with asyncio.timeout(2):
        await condition.wait()


async def _stop(runtime: BotRuntime, polling: asyncio.Task[None]) -> None:
    async with asyncio.timeout(2):
        await runtime.dp.stop_polling()
        await polling


async def test_polling_drops_the_webhook_and_asks_only_for_handled_updates(
    settings: Config, i18n: I18n
) -> None:
    session = PollingSession(build_message_update("/start"))
    stubs = Stubs(session)
    config = configured(settings)
    async with bot_runtime(config, TestOverrides(session, i18n), stubs) as runtime:
        polling = asyncio.create_task(poll(runtime))
        await session.wait_for(SendMessage)
        await _stop(runtime, polling)
    [delete] = session.sent(DeleteWebhook)
    # Updates sent while switching modes are kept.
    assert not delete.drop_pending_updates
    first = session.sent(GetUpdates)[0]
    assert set(first.allowed_updates or ()) == {
        "message",
        "callback_query",
        "my_chat_member",
    }


async def test_health_is_served_while_polling(settings: Config, i18n: I18n) -> None:
    session = PollingSession()
    config = configured(settings)
    async with bot_runtime(
        config, TestOverrides(session, i18n), Stubs(session)
    ) as runtime:
        polling = asyncio.create_task(poll(runtime))
        url = f"http://127.0.0.1:{config.web_server.WEB_SERVER_PORT}/live"
        async with asyncio.timeout(2), aiohttp.ClientSession() as client:
            while True:
                try:
                    async with client.get(url) as response:
                        assert response.status == 200
                        break
                except aiohttp.ClientConnectionError:
                    await asyncio.sleep(0.01)
        await _stop(runtime, polling)


async def test_polling_leaves_the_bot_session_to_the_container(
    settings: Config, i18n: I18n
) -> None:
    session = PollingSession()
    session.close = AsyncMock()
    config = configured(settings)
    async with bot_runtime(
        config, TestOverrides(session, i18n), Stubs(session)
    ) as runtime:
        polling = asyncio.create_task(poll(runtime))
        await session.wait_for(GetUpdates)
        await _stop(runtime, polling)
        session.close.assert_not_awaited()  # the container owns the bot session
    session.close.assert_awaited_once()


async def test_stopping_lets_an_update_in_progress_finish(
    settings: Config, i18n: I18n
) -> None:
    gate = Gate()
    session = PollingSession(build_message_update("/start"))
    stubs = Stubs(session, gate=gate)
    config = configured(settings)
    async with bot_runtime(config, TestOverrides(session, i18n), stubs) as runtime:
        polling = asyncio.create_task(poll(runtime))
        await _until(gate.entered)
        stopping = asyncio.create_task(runtime.dp.stop_polling())
        await asyncio.sleep(0.05)
        assert not polling.done()  # still waiting for the update
        gate.opened.set()
        async with asyncio.timeout(2):
            await asyncio.gather(stopping, polling)
    assert len(session.sent(SendMessage)) == 1
    assert not gate.cancelled
    # The dispatcher closed the FSM storage only after the reply went out.
    assert stubs.storage.replies_at_close == [1]


async def test_stopping_gives_up_on_a_stuck_update(
    settings: Config, i18n: I18n
) -> None:
    gate = Gate()
    session = PollingSession(build_message_update("/start"))
    config = configured(settings, shutdown_timeout=0.05)
    with captured_logs("src.main.runtime") as records:
        async with bot_runtime(
            config, TestOverrides(session, i18n), Stubs(session, gate=gate)
        ) as runtime:
            polling = asyncio.create_task(poll(runtime))
            await _until(gate.entered)
            await _stop(runtime, polling)
    assert gate.cancelled
    assert session.sent(SendMessage) == []
    assert any(
        r.levelno == logging.WARNING and "Cancelling 1 update" in r.getMessage()
        for r in records
    )
