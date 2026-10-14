from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sentry_sdk.integrations.asyncio import AsyncioIntegration

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
