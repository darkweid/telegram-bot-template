from redis.asyncio import Redis


def create_redis_client(
    connection_url: str,
    *,
    socket_timeout: float = 5.0,
    socket_connect_timeout: float = 5.0,
    health_check_interval: int = 30,
) -> Redis:
    """Connects lazily, on first command.

    Decoding is not a parameter: every caller compares replies against str, and a
    bytes reply compares unequal without an error. No caller may ask for the other one.
    """
    return Redis.from_url(
        connection_url,
        decode_responses=True,
        socket_timeout=socket_timeout,
        socket_connect_timeout=socket_connect_timeout,
        health_check_interval=health_check_interval,
    )
