import logging
import re
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.aiohttp import AioHttpIntegration
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from loggers import get_logger
from src.main.config import Config

logger = get_logger(__name__)

_sentry_initialized = False

# The Bot API carries the token in the URL path, so any URL, exception message
# or log line that mentions a request leaks it.
_BOT_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
_SECRET_HEADERS = {"x-telegram-bot-api-secret-token"}


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _BOT_TOKEN_IN_URL.sub("/bot[Filtered]", value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item) for item in value)
    return value


def _drop_secret_headers(event: dict[str, Any]) -> None:
    request = event.get("request")
    if not isinstance(request, dict):
        return
    headers = request.get("headers")
    if isinstance(headers, dict):
        request["headers"] = {
            name: value
            for name, value in headers.items()
            if str(name).lower() not in _SECRET_HEADERS
        }
    elif isinstance(headers, list):
        request["headers"] = [
            pair for pair in headers if str(pair[0]).lower() not in _SECRET_HEADERS
        ]


def scrub_event(event: Any, hint: Any) -> Any:
    """Remove bot tokens from every string of an event and drop the webhook
    secret header, before the event leaves the process."""
    scrubbed: dict[str, Any] = _scrub(event)
    _drop_secret_headers(scrubbed)
    return scrubbed


def scrub_breadcrumb(crumb: Any, hint: Any) -> Any:
    return _scrub(crumb)


def init_sentry(config: Config) -> bool:
    """
    Initialize the Sentry client once; return True when this call did it.
    """
    global _sentry_initialized

    if _sentry_initialized:
        return False

    if config.app.DEBUG or config.app.TESTING:
        logger.info("DEBUG/TESTING enabled, skipping Sentry initialization")
        return False

    if not config.sentry.SENTRY_ENABLED or not config.sentry.SENTRY_DSN:
        logger.info("Sentry disabled or DSN empty, skipping Sentry initialization")
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
        before_send=scrub_event,
        before_send_transaction=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
        integrations=[
            LoggingIntegration(
                level=logging.INFO,  # breadcrumbs from INFO and up
                # Only CRITICAL logs become Sentry events; lower levels need an
                # explicit capture.
                event_level=logging.CRITICAL,
            ),
            # An exception in a task nobody awaits (a handler spawned with
            # create_task) would otherwise vanish.
            AsyncioIntegration(),
        ],
        # aiogram's HTTP client is aiohttp: its integration would record every
        # Bot API call, token URL included, as a breadcrumb and a span.
        disabled_integrations=[AioHttpIntegration()],
    )
    _sentry_initialized = True
    logger.info("Sentry initialized")
    return True
