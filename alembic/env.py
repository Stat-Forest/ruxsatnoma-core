"""Alembic environment for the core service.

Runs migrations through the async engine, because the service has one driver
and one URL and there is no reason to keep a second, synchronous one.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, MetaData, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from core.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The owner of the models attaches their metadata here once tables exist:
#     from core.modules.geo.models import Base
#     target_metadata = Base.metadata
# Until then autogenerate has nothing to compare against, which is correct:
# the schema is written by hand and reviewed, not guessed.
target_metadata: MetaData | None = None


def get_url() -> str:
    """Return the URL to migrate: the one passed in, otherwise the settings."""
    configured = config.get_main_option("sqlalchemy.url")
    return configured or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations on an already established connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Connect with the async engine and hand the connection to Alembic."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()
    engine = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """Entry point for the usual, connected run."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
