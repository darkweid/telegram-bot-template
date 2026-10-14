from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.database import engine as engine_module
from src.core.database.engine import create_engine, session_timeouts
from src.main.config import Config


async def test_engine_is_built_from_the_given_settings(settings: Config) -> None:
    engine = create_engine(
        settings.postgres.dsn_async,
        echo=False,
        pool_size=3,
        max_overflow=2,
        statement_timeout_seconds=1.5,
        idle_in_transaction_timeout_seconds=0,
    )
    try:
        assert isinstance(engine, AsyncEngine)
        assert (
            engine.url.render_as_string(hide_password=False)
            == settings.postgres.dsn_async
        )
        assert engine.pool.size() == 3
        assert engine.pool._max_overflow == 2  # no public accessor
        assert engine.sync_engine.hide_parameters is True
    finally:
        await engine.dispose()


def test_engine_passes_timeouts_and_pool_limits_to_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def spy(dsn: str, **kwargs: Any) -> AsyncEngine:
        captured["dsn"] = dsn
        captured.update(kwargs)
        return create_async_engine(dsn, **kwargs)

    monkeypatch.setattr(engine_module, "create_async_engine", spy)

    create_engine(
        "postgresql+asyncpg://u:p@localhost:5432/db",
        echo=True,
        pool_size=3,
        max_overflow=2,
        statement_timeout_seconds=1.5,
        idle_in_transaction_timeout_seconds=0,
    )

    assert captured["dsn"] == "postgresql+asyncpg://u:p@localhost:5432/db"
    assert captured["echo"] is True
    assert captured["hide_parameters"] is True
    assert captured["pool_size"] == 3
    assert captured["max_overflow"] == 2
    assert captured["pool_timeout"] == 30
    assert captured["pool_recycle"] == 60 * 30
    assert captured["pool_pre_ping"] is True
    assert captured["connect_args"] == {
        "server_settings": {
            "statement_timeout": "1500",
            "idle_in_transaction_session_timeout": "0",
        }
    }


def test_session_timeouts_are_whole_milliseconds_rounded_up() -> None:
    assert session_timeouts(statement_timeout_seconds=0.0001) == {
        "server_settings": {
            "statement_timeout": "1",
            "idle_in_transaction_session_timeout": "0",
        }
    }


def test_importing_the_module_builds_no_engine() -> None:
    import src.core.database.engine as module

    assert not any(isinstance(value, AsyncEngine) for value in vars(module).values())
