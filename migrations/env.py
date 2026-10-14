import asyncio

from alembic import context
from alembic.config import Config
import alembic_postgresql_enum  # noqa: F401 - registers the enum comparators with autogenerate
from sqlalchemy import MetaData
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import models  # noqa: F401 - populates Base.metadata for autogenerate
from src.core.database.base import Base
from src.main.config import get_settings

config: Config = context.config
target_metadata: MetaData = Base.metadata


def _get_postgres_dsn() -> str:
    return get_settings().postgres.dsn_async


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: emits SQL to stdout without connecting."""
    context.configure(
        url=_get_postgres_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """The application's async engine, with parameters hidden from error output."""
    connectable: AsyncEngine = create_async_engine(
        url=_get_postgres_dsn(), hide_parameters=True
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
