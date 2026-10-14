"""`python -m src.main`: run the bot in the mode BOT_MODE names."""

import asyncio

from loggers import get_logger, setup_logging
from src.main.config import Config, get_settings
from src.main.polling import poll
from src.main.runtime import DependencyUnavailableError, bot_runtime
from src.main.sentry import init_sentry
from src.main.webhook import serve_webhook

logger = get_logger(__name__)


async def run(config: Config) -> None:
    # Inside the loop: Sentry's asyncio integration patches the running loop's
    # task factory, and without a loop it silently patches nothing.
    init_sentry(config)
    async with bot_runtime(config) as runtime:
        me = await runtime.bot.me()
        logger.info("Starting @%s in %s mode", me.username, config.bot.BOT_MODE)
        if config.bot.BOT_MODE == "webhook":
            await serve_webhook(runtime)
        else:
            await poll(runtime)


def main() -> None:
    setup_logging()
    config = get_settings()
    try:
        asyncio.run(run(config))
    except DependencyUnavailableError as exc:
        # One line, no traceback: the cause is outside the code.
        logger.critical("Cannot start: %s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
