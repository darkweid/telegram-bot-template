from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

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
        # One immediate retry, so a pooled connection that Redis dropped (a
        # restart) reconnects instead of failing the command. No more: a hung
        # Redis then costs at most about two socket timeouts per command (10 s
        # by default), which stays well under the 60 s event-isolation lock and
        # DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS. TouchUserUseCase writes the
        # user's snapshot while it holds the users row lock and relies on it.
        retry=Retry(NoBackoff(), retries=1),
    )
