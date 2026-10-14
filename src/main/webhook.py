from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from src.main.health import create_web_app
from src.main.runtime import (
    BotRuntime,
    finish_updates,
    serving,
    wait_for_stop_signal,
)


class GracefulRequestHandler(SimpleRequestHandler):
    """aiogram's handler: checks the secret, answers Telegram at once and
    handles the update in the background. Differs on shutdown: it waits for
    those background updates, and leaves the bot session to the container."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        *,
        secret_token: str,
        shutdown_timeout: float,
    ) -> None:
        super().__init__(dispatcher=dispatcher, bot=bot, secret_token=secret_token)
        self.shutdown_timeout = shutdown_timeout

    async def close(self) -> None:
        # The base class closes the bot session here, under updates still
        # running. tests/unit/src/main/test_webhook.py pins the attribute.
        await finish_updates(self._background_feed_update_tasks, self.shutdown_timeout)


def create_webhook_app(runtime: BotRuntime) -> web.Application:
    """The health application plus the Telegram route."""
    secret = runtime.config.webhook.WEBHOOK_SECRET.get_secret_value()
    if not secret:
        # Config refuses webhook mode without a secret; this guards the one
        # place where its absence would be silent: aiogram accepts any request
        # when the handler's secret is empty.
        raise RuntimeError("WEBHOOK_SECRET is required to serve the webhook")
    app = create_web_app(runtime.container)
    handler = GracefulRequestHandler(
        runtime.dp,
        runtime.bot,
        secret_token=secret,
        shutdown_timeout=runtime.config.bot.SHUTDOWN_TIMEOUT_SECONDS,
    )
    # Before setup_application: aiohttp runs on_shutdown callbacks in order,
    # so updates in progress finish before the dispatcher shuts down.
    handler.register(app, path=runtime.config.webhook.WEBHOOK_PATH)
    setup_application(app, runtime.dp, bot=runtime.bot)
    return app


async def register_webhook(runtime: BotRuntime) -> None:
    """Point Telegram at this deployment.

    Set on every start (setWebhook is idempotent), so a changed URL, secret or
    update list takes effect with the deploy. Never deleted on shutdown:
    Telegram queues updates while the next container starts.
    """
    webhook = runtime.config.webhook
    await runtime.bot.set_webhook(
        url=webhook.url,
        secret_token=webhook.WEBHOOK_SECRET.get_secret_value(),
        allowed_updates=runtime.dp.resolve_used_update_types(),
        max_connections=webhook.WEBHOOK_MAX_CONNECTIONS,
        drop_pending_updates=False,
    )


async def serve_webhook(
    runtime: BotRuntime, until: Callable[[], Awaitable[None]] = wait_for_stop_signal
) -> None:
    """Serve the webhook and health until `until` returns (SIGTERM or SIGINT)."""
    async with serving(create_webhook_app(runtime), runtime.config.web_server):
        # Registered once the server listens, so Telegram's first delivery
        # does not meet a closed port.
        await register_webhook(runtime)
        await until()
