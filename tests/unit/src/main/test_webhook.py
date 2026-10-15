import asyncio
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage, SetWebhook
from aiogram.types import Update
from aiogram.utils.i18n import I18n
import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest

from src.main.config import Config
from src.main.runtime import bot_runtime
from src.main.webhook import (
    WebhookRegistrationError,
    create_webhook_app,
    register_webhook,
    serve_webhook,
)
from tests.factories.updates import build_message_update
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.providers import TestOverrides
from tests.helpers.runtime import WEBHOOK_SECRET, Gate, HeldWork, Stubs, configured

PATH = "/webhook/telegram"


def _body(update: Update) -> dict[str, object]:
    return update.model_dump(mode="json", by_alias=True, exclude_none=True)


async def _post(
    client: TestClient, update: Update, secret: str | None
) -> aiohttp.ClientResponse:
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    return await client.post(PATH, json=_body(update), headers=headers)


@pytest.mark.parametrize("secret", [None, "", "x" * 32, WEBHOOK_SECRET[:-1]])
async def test_a_request_without_the_right_secret_is_refused(
    settings: Config, i18n: I18n, secret: str | None
) -> None:
    session = FakeTelegramSession()
    stubs = Stubs(session)
    config = configured(settings, webhook=True)
    async with bot_runtime(config, TestOverrides(session, i18n), stubs) as runtime:
        async with TestClient(TestServer(create_webhook_app(runtime))) as client:
            response = await _post(client, build_message_update("/start"), secret)
            assert response.status == 401
            await asyncio.sleep(0.05)
    assert session.sent(SendMessage) == []


async def test_an_update_is_acknowledged_first_and_handled_in_the_background(
    settings: Config, i18n: I18n
) -> None:
    gate = Gate()
    session = FakeTelegramSession()
    config = configured(settings, webhook=True)
    stubs = Stubs(session, gate=gate)
    async with bot_runtime(config, TestOverrides(session, i18n), stubs) as runtime:
        async with TestClient(TestServer(create_webhook_app(runtime))) as client:
            response = await _post(
                client, build_message_update("/start"), WEBHOOK_SECRET
            )
            # Answered while the update is still held at the gate.
            assert response.status == 200
            assert session.sent(SendMessage) == []
            gate.opened.set()
            await session.wait_for(SendMessage)


async def test_shutdown_lets_an_update_in_progress_finish(
    settings: Config, i18n: I18n
) -> None:
    gate = Gate()
    session = FakeTelegramSession()
    session.close = AsyncMock()
    stubs = Stubs(session, gate=gate)
    config = configured(settings, webhook=True)
    async with bot_runtime(config, TestOverrides(session, i18n), stubs) as runtime:
        client = TestClient(TestServer(create_webhook_app(runtime)))
        await client.start_server()
        await _post(client, build_message_update("/start"), WEBHOOK_SECRET)
        async with asyncio.timeout(2):
            await gate.entered.wait()
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0.05)
        assert not closing.done()  # still waiting for the update
        gate.opened.set()
        async with asyncio.timeout(2):
            await closing
        assert stubs.storage.replies_at_close == [1]
        session.close.assert_not_awaited()  # the container owns the bot session
    session.close.assert_awaited_once()
    assert not gate.cancelled


async def test_shutdown_gives_up_on_a_stuck_update(
    settings: Config, i18n: I18n
) -> None:
    gate = Gate()
    session = FakeTelegramSession()
    config = configured(settings, webhook=True, shutdown_timeout=0.05)
    stubs = Stubs(session, gate=gate)
    async with bot_runtime(config, TestOverrides(session, i18n), stubs) as runtime:
        client = TestClient(TestServer(create_webhook_app(runtime)))
        await client.start_server()
        await _post(client, build_message_update("/start"), WEBHOOK_SECRET)
        async with asyncio.timeout(2):
            await gate.entered.wait()
            await client.close()
    assert gate.cancelled
    assert session.sent(SendMessage) == []


async def test_shutdown_waits_for_background_work(settings: Config, i18n: I18n) -> None:
    """In a webhook roll the new bot already takes the traffic; the old one
    still owes the user the answer its work is preparing."""
    work = HeldWork()
    session = FakeTelegramSession()
    session.close = AsyncMock()
    stubs = Stubs(session)
    config = configured(settings, webhook=True)
    async with bot_runtime(config, TestOverrides(session, i18n), stubs) as runtime:
        client = TestClient(TestServer(create_webhook_app(runtime)))
        await client.start_server()
        runtime.background.start("probe", work)
        async with asyncio.timeout(2):
            await work.started.wait()
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0.05)
        assert not closing.done()  # still waiting for the work
        session.close.assert_not_awaited()
        work.released.set()
        async with asyncio.timeout(2):
            await closing
    assert work.outcome == "finished"


async def test_shutdown_cancels_background_work_past_the_timeout(
    settings: Config, i18n: I18n
) -> None:
    work = HeldWork()
    session = FakeTelegramSession()
    config = configured(settings, webhook=True, shutdown_timeout=0.05)
    async with bot_runtime(
        config, TestOverrides(session, i18n), Stubs(session)
    ) as runtime:
        client = TestClient(TestServer(create_webhook_app(runtime)))
        await client.start_server()
        runtime.background.start("probe", work)
        async with asyncio.timeout(2):
            await work.started.wait()
            await client.close()
    assert work.outcome == "cancelled"


async def test_the_webhook_app_serves_health_too(settings: Config, i18n: I18n) -> None:
    session = FakeTelegramSession()
    config = configured(settings, webhook=True)
    async with bot_runtime(
        config, TestOverrides(session, i18n), Stubs(session)
    ) as runtime:
        async with TestClient(TestServer(create_webhook_app(runtime))) as client:
            assert (await client.get("/live")).status == 200


async def test_the_webhook_is_set_with_secret_updates_and_limit(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    config = configured(settings, webhook=True)
    async with bot_runtime(
        config, TestOverrides(session, i18n), Stubs(session)
    ) as runtime:
        await register_webhook(runtime)
    [call] = session.sent(SetWebhook)
    assert call.url == "https://bot.example.com/webhook/telegram"
    assert call.secret_token == WEBHOOK_SECRET
    assert set(call.allowed_updates or ()) == {
        "message",
        "callback_query",
        "my_chat_member",
    }
    assert call.max_connections == config.webhook.WEBHOOK_MAX_CONNECTIONS
    assert not call.drop_pending_updates


async def test_serve_webhook_listens_until_told_to_stop(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    config = configured(settings, webhook=True)
    stop = asyncio.Event()
    async with bot_runtime(
        config, TestOverrides(session, i18n), Stubs(session)
    ) as runtime:
        serving = asyncio.create_task(serve_webhook(runtime, until=stop.wait))
        url = f"http://127.0.0.1:{config.web_server.WEB_SERVER_PORT}/live"
        async with asyncio.timeout(2), aiohttp.ClientSession() as client:
            await session.wait_for(SetWebhook)
            async with client.get(url) as response:
                assert response.status == 200
        stop.set()
        async with asyncio.timeout(2):
            await serving


async def test_an_empty_secret_never_serves_the_webhook(
    settings: Config, i18n: I18n
) -> None:
    session = FakeTelegramSession()
    async with bot_runtime(
        configured(settings), TestOverrides(session, i18n), Stubs(session)
    ) as runtime:
        with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
            create_webhook_app(runtime)


async def test_a_refused_webhook_becomes_a_startup_error(
    settings: Config, i18n: I18n
) -> None:
    """Telegram's refusal is an environment problem (DNS, certificate, port);
    it must stop the process with Telegram's reason, not a traceback."""
    session = FakeTelegramSession()
    session.queue(
        SetWebhook,
        TelegramBadRequest(
            method=SetWebhook(url="https://bot.example.com/webhook/telegram"),
            message="Bad Request: bad webhook: Failed to resolve host",
        ),
    )
    config = configured(settings, webhook=True)
    async with bot_runtime(
        config, TestOverrides(session, i18n), Stubs(session)
    ) as runtime:
        with pytest.raises(WebhookRegistrationError) as caught:
            await register_webhook(runtime)
    assert str(caught.value) == (
        "Cannot set the webhook (TelegramBadRequest): "
        "Bad Request: bad webhook: Failed to resolve host"
    )
    assert caught.value.__cause__ is None
    assert WEBHOOK_SECRET not in str(caught.value)


async def test_a_network_failure_names_its_class_and_leaks_no_secret(
    settings: Config, i18n: I18n
) -> None:
    """The line must not call a dropped connection a refusal, and the failed
    request carries the bot token and the webhook secret, which stay out."""
    session = FakeTelegramSession()
    session.queue(
        SetWebhook,
        TelegramNetworkError(
            method=SetWebhook(
                url="https://bot.example.com/webhook/telegram",
                secret_token=WEBHOOK_SECRET,
            ),
            message="Request timeout error",
        ),
    )
    config = configured(settings, webhook=True)
    async with bot_runtime(
        config, TestOverrides(session, i18n), Stubs(session)
    ) as runtime:
        with pytest.raises(WebhookRegistrationError) as caught:
            await register_webhook(runtime)
    message = str(caught.value)
    assert message.startswith("Cannot set the webhook (TelegramNetworkError): ")
    assert caught.value.__cause__ is None
    assert WEBHOOK_SECRET not in message
    assert config.bot.BOT_TOKEN.get_secret_value() not in message
