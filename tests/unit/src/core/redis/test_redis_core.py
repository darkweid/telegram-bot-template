from src.core.redis.core import create_redis_client


def test_a_client_decodes_responses() -> None:
    """Every caller compares replies against str, and a bytes reply compares
    unequal without an error."""
    client = create_redis_client("redis://localhost:6379/0")

    assert client.connection_pool.connection_kwargs["decode_responses"] is True
