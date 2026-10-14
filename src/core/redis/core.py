from redis.asyncio import Redis

# Every Redis connection of the project, the task broker's included: a
# blackholed Redis must fail a command in seconds.
REDIS_SOCKET_TIMEOUT_SECONDS = 5.0


def create_redis_client(
    connection_url: str,
    *,
    socket_timeout: float = REDIS_SOCKET_TIMEOUT_SECONDS,
    socket_connect_timeout: float = REDIS_SOCKET_TIMEOUT_SECONDS,
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
