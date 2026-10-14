import logging

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from loggers import get_logger
from src.main.config import Config

logger = get_logger(__name__)

_sentry_initialized = False


def init_sentry(config: Config) -> bool:
    """
    Initialize the Sentry client once; return True when this call did it.
    """
    global _sentry_initialized

    if _sentry_initialized:
        return False

    if config.app.DEBUG or config.app.TESTING:
        logger.info("DEBUG/TESTING enabled. Skipping Sentry initialization.")
        return False

    if not config.sentry.SENTRY_ENABLED or not config.sentry.SENTRY_DSN:
        logger.info("Sentry disabled or DSN empty. Skipping Sentry initialization.")
        return False

    sentry_sdk.init(
        dsn=config.sentry.SENTRY_DSN,
        environment=config.sentry.SENTRY_ENV,
        release=config.app.VERSION,
        # Frame locals and payloads carry what users typed; the default scrubber
        # only matches exact key names at the top level, and send_default_pii
        # covers neither.
        include_local_variables=False,
        max_request_body_size="never",
        integrations=[
            LoggingIntegration(
                level=logging.INFO,  # breadcrumbs from INFO and up
                event_level=logging.CRITICAL,  # only CRITICAL+ logs become Sentry events; lower levels require explicit capture
            ),
            # An exception in a task nobody awaits (a handler spawned with create_task) would otherwise vanish.
            AsyncioIntegration(),
        ],
    )
    _sentry_initialized = True
    logger.info("Sentry initialized.")
    return True
