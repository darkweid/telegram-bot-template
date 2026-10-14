from src.main.health import create_web_app
from src.main.runtime import BotRuntime, finish_updates, serving


async def poll(runtime: BotRuntime) -> None:
    """Long-poll until SIGTERM or SIGINT, with the health server alongside.

    Strictly one polling instance per bot token: a second one makes Telegram
    answer both with 409 Conflict. Deploys recreate the container.
    """
    dp, bot, config = runtime.dp, runtime.bot, runtime.config
    # A webhook left from webhook mode makes getUpdates fail with 409. Updates
    # queued meanwhile are kept: the user is waiting for an answer to them.
    await bot.delete_webhook(drop_pending_updates=False)

    async def finish_in_progress() -> None:
        # start_polling hands each update to a task of its own and returns
        # without waiting for them; tests/unit/src/main/test_polling.py pins
        # the attribute this relies on.
        await finish_updates(
            dp._handle_update_tasks, config.bot.SHUTDOWN_TIMEOUT_SECONDS
        )

    dp.shutdown.register(finish_in_progress)
    # First among the shutdown handlers: Dispatcher.__init__ registers closing
    # the FSM storage, and the updates still finishing need it.
    dp.shutdown.handlers.insert(0, dp.shutdown.handlers.pop())

    async with serving(create_web_app(runtime.container), config.web_server):
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            # The container owns the bot session and closes it last.
            close_bot_session=False,
        )
