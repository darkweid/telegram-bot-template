import math
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

POOL_TIMEOUT_SECONDS = 30
POOL_RECYCLE_SECONDS = 60 * 30


def session_timeouts(
    *,
    statement_timeout_seconds: float = 0,
    idle_in_transaction_timeout_seconds: float = 0,
) -> dict[str, Any]:
    """
    asyncpg `connect_args` that set session-level timeouts at connection startup.

    They are session defaults, so `set_local_statement_timeout` still raises
    or lowers the statement cap for one transaction. Both are always sent: a 0
    goes out as "0", so "no limit" holds even where the server has a global
    timeout of its own (ALTER SYSTEM, postgresql.conf). Values are whole
    milliseconds, rounded up so a sub-millisecond setting does not become 0 and
    switch the limit off.
    """
    settings = {
        name: str(math.ceil(seconds * 1000))
        for name, seconds in (
            ("statement_timeout", statement_timeout_seconds),
            (
                "idle_in_transaction_session_timeout",
                idle_in_transaction_timeout_seconds,
            ),
        )
    }
    return {"server_settings": settings}


def create_engine(
    dsn: str,
    *,
    echo: bool,
    pool_size: int,
    max_overflow: int,
    statement_timeout_seconds: float,
    idle_in_transaction_timeout_seconds: float,
) -> AsyncEngine:
    """The bot's connection pool. One per process, owned by the container,
    which disposes of it on shutdown. Migrations build their own engine
    (migrations/env.py) without these limits.

    Takes plain values rather than the config section: src.core never
    imports the composition root (.importlinter)."""
    return create_async_engine(
        dsn,
        echo=echo,
        # A driver error otherwise renders its bound values into str(exc)
        # ("[parameters: ...]"), which reaches logs and Sentry: usernames,
        # message text. Every engine, migrations included, hides them.
        hide_parameters=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_pre_ping=True,
        connect_args=session_timeouts(
            statement_timeout_seconds=statement_timeout_seconds,
            idle_in_transaction_timeout_seconds=idle_in_transaction_timeout_seconds,
        ),
    )
