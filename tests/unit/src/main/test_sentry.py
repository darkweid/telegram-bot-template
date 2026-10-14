from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sentry_sdk.integrations.aiohttp import AioHttpIntegration
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.transport import Transport

from src.main.config import Config
import src.main.sentry as sentry_module


@pytest.fixture(autouse=True)
def reset_sentry_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sentry_module, "_sentry_initialized", False)


def _build(
    settings: Config,
    *,
    debug: bool = False,
    testing: bool = False,
    enabled: bool = True,
    dsn: str | None = "http://example.com",
    version: str | None = None,
    **sentry_extra: Any,
) -> Config:
    app_update: dict[str, Any] = {"DEBUG": debug, "TESTING": testing}
    if version is not None:
        app_update["VERSION"] = version
    return settings.model_copy(
        update={
            "app": settings.app.model_copy(update=app_update),
            "sentry": settings.sentry.model_copy(
                update={"SENTRY_ENABLED": enabled, "SENTRY_DSN": dsn, **sentry_extra}
            ),
        }
    )


def _enabled(settings: Config) -> Config:
    return _build(settings)


def test_init_sentry_skips_when_debug(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    init_mock = MagicMock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", init_mock)

    assert sentry_module.init_sentry(_build(settings, debug=True)) is False

    init_mock.assert_not_called()
    assert sentry_module._sentry_initialized is False


def test_init_sentry_skips_when_testing(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    init_mock = MagicMock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", init_mock)

    assert sentry_module.init_sentry(_build(settings, testing=True)) is False

    init_mock.assert_not_called()
    assert sentry_module._sentry_initialized is False


def test_init_sentry_skips_when_disabled(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    init_mock = MagicMock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", init_mock)

    assert sentry_module.init_sentry(_build(settings, enabled=False)) is False

    init_mock.assert_not_called()
    assert sentry_module._sentry_initialized is False


def test_init_sentry_skips_when_dsn_missing(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    init_mock = MagicMock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", init_mock)

    assert sentry_module.init_sentry(_build(settings, dsn=None)) is False

    init_mock.assert_not_called()
    assert sentry_module._sentry_initialized is False


def test_init_sentry_initializes_once(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    init_mock = MagicMock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", init_mock)
    config = _build(settings, version="1.2.3", SENTRY_ENV="test")

    assert sentry_module.init_sentry(config) is True
    assert sentry_module.init_sentry(config) is False

    init_mock.assert_called_once()
    assert init_mock.call_args.kwargs["environment"] == "test"
    assert init_mock.call_args.kwargs["release"] == "1.2.3"
    assert sentry_module._sentry_initialized is True


def test_init_sentry_sends_no_frame_locals_or_payloads(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    """Locals and payloads of a failing handler hold what users typed and the
    key-name scrubber misses it; send_default_pii=False does not drop them."""
    init_mock = MagicMock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", init_mock)

    sentry_module.init_sentry(_enabled(settings))

    kwargs = init_mock.call_args.kwargs
    assert kwargs["include_local_variables"] is False
    assert kwargs["max_request_body_size"] == "never"
    assert kwargs.get("send_default_pii", False) is False


def test_sentry_gets_the_asyncio_integration(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    init_mock = MagicMock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", init_mock)

    assert sentry_module.init_sentry(_enabled(settings)) is True

    integrations = init_mock.call_args.kwargs["integrations"]
    assert any(isinstance(i, AsyncioIntegration) for i in integrations)


TOKEN = "123456789:AAtest-bot-token-not-real-0000000000000"
TOKEN_URL = f"https://api.telegram.org/bot{TOKEN}/getMe"


def _init_kwargs(monkeypatch: pytest.MonkeyPatch, settings: Config) -> dict[str, Any]:
    init_mock = MagicMock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", init_mock)
    sentry_module.init_sentry(_enabled(settings))
    kwargs: dict[str, Any] = init_mock.call_args.kwargs
    return kwargs


def test_aiohttp_integration_is_disabled(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    kwargs = _init_kwargs(monkeypatch, settings)

    assert any(
        isinstance(i, AioHttpIntegration) for i in kwargs["disabled_integrations"]
    )
    assert not any(isinstance(i, AioHttpIntegration) for i in kwargs["integrations"])


def test_breadcrumb_hook_scrubs_the_token(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    kwargs = _init_kwargs(monkeypatch, settings)
    crumb = {
        "category": "httplib",
        "message": f"POST {TOKEN_URL}",
        "data": {"url": TOKEN_URL, "status_code": 200},
    }

    scrubbed = kwargs["before_breadcrumb"](crumb, {})

    assert TOKEN not in str(scrubbed)
    assert scrubbed["data"]["url"] == "https://api.telegram.org/bot[Filtered]/getMe"
    assert scrubbed["data"]["status_code"] == 200


@pytest.mark.parametrize("hook", ["before_send", "before_send_transaction"])
def test_event_hooks_scrub_urls_messages_and_spans(
    monkeypatch: pytest.MonkeyPatch, settings: Config, hook: str
) -> None:
    kwargs = _init_kwargs(monkeypatch, settings)
    event = {
        "request": {"url": TOKEN_URL},
        "exception": {"values": [{"value": f"Cannot connect to {TOKEN_URL}"}]},
        "breadcrumbs": {"values": [{"data": {"url": TOKEN_URL}}]},
        "spans": [{"description": f"GET {TOKEN_URL}", "data": {"url": TOKEN_URL}}],
    }

    scrubbed = kwargs[hook](event, {})

    assert TOKEN not in str(scrubbed)
    assert "/bot[Filtered]/getMe" in scrubbed["request"]["url"]


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Telegram-Bot-Api-Secret-Token": "s3cret", "Host": "bot.example"},
        [["x-telegram-bot-api-secret-token", "s3cret"], ["Host", "bot.example"]],
    ],
)
def test_before_send_drops_the_webhook_secret_header(
    monkeypatch: pytest.MonkeyPatch, settings: Config, headers: Any
) -> None:
    kwargs = _init_kwargs(monkeypatch, settings)

    scrubbed = kwargs["before_send"]({"request": {"headers": headers}}, {})

    assert "s3cret" not in str(scrubbed)
    assert "bot.example" in str(scrubbed)


def test_real_client_drops_the_aiohttp_integration_and_scrubs_the_token(
    monkeypatch: pytest.MonkeyPatch, settings: Config
) -> None:
    """Runs the real sentry_sdk.init: the kwargs tests above cannot tell
    whether the SDK honours them."""
    sent: list[Any] = []

    class CapturingTransport(Transport):
        def capture_envelope(self, envelope: Any) -> None:
            sent.extend(item.payload.json for item in envelope.items)

    real_init = sentry_module.sentry_sdk.init
    monkeypatch.setattr(
        sentry_module.sentry_sdk,
        "init",
        lambda **kwargs: real_init(**kwargs, transport=CapturingTransport),
    )
    config = _build(settings, dsn="https://key@example.invalid/1")

    try:
        assert sentry_module.init_sentry(config) is True
        assert "aiohttp" not in sentry_module.sentry_sdk.get_client().integrations

        sentry_module.sentry_sdk.capture_message(f"failed: {TOKEN_URL}")
        sentry_module.sentry_sdk.flush()
    finally:
        sentry_module.sentry_sdk.get_client().close()
        sentry_module.sentry_sdk.get_global_scope().set_client(None)

    assert sent
    assert TOKEN not in str(sent)
    assert "/bot[Filtered]/getMe" in str(sent)
