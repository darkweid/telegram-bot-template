from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.redis import lifecycle


async def test_verify_redis_client_accepts_a_successful_ping() -> None:
    client = SimpleNamespace(ping=AsyncMock(return_value=True))

    await lifecycle.verify_redis_client(client)

    client.ping.assert_awaited_once()


async def test_verify_redis_client_raises_when_ping_fails() -> None:
    """An unreachable Redis must fail startup, not every update after it."""
    client = SimpleNamespace(ping=AsyncMock(return_value=False))

    with pytest.raises(RuntimeError, match="Redis ping failed"):
        await lifecycle.verify_redis_client(client)
