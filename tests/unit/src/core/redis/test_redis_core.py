import asyncio

import pytest
import redis.exceptions as redis_exc

from src.core.redis.core import create_redis_client


def test_a_client_decodes_responses() -> None:
    """Every caller compares replies against str, and a bytes reply compares
    unequal without an error."""
    client = create_redis_client("redis://localhost:6379/0")

    assert client.connection_pool.connection_kwargs["decode_responses"] is True


def test_a_command_is_retried_once() -> None:
    """TouchUserUseCase writes to the cache under a users row lock: every
    further retry adds a socket timeout to how long a hung Redis holds it."""
    client = create_redis_client("redis://localhost:6379/0")

    assert client.connection_pool.make_connection().retry.get_retries() == 1


async def test_a_hung_redis_fails_a_command_after_two_attempts() -> None:
    """A cache outage must fail open within seconds, not stall the update past
    the event-isolation lock and the idle-in-transaction timeout."""
    connections: list[asyncio.StreamWriter] = []

    async def accept_and_never_answer(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connections.append(writer)

    server = await asyncio.start_server(accept_and_never_answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = create_redis_client(
        f"redis://127.0.0.1:{port}/0",
        socket_timeout=0.1,
        socket_connect_timeout=0.1,
        health_check_interval=0,
    )
    try:
        with pytest.raises(redis_exc.TimeoutError):
            await client.get("key")
    finally:
        await client.aclose()
        for writer in connections:
            writer.close()
        server.close()
        await server.wait_closed()

    assert len(connections) == 2
