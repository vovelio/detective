"""Async migrations; the operator runs upgrades explicitly before serving traffic."""

import asyncio
import os
import re

from alembic import context
from detective.api.config import secret_value
from detective.api.db import Base
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

schema = os.environ.get("DATABASE_SCHEMA", "public")
if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
    raise ValueError("Invalid DATABASE_SCHEMA")
url = secret_value("DATABASE_URL")
if not url.startswith("postgresql+asyncpg://"):
    raise ValueError("PostgreSQL with asyncpg is required")


def offline() -> None:
    context.configure(
        url=url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    engine = create_async_engine(
        url,
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema}},
    )

    def migrate(connection: Connection) -> None:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema=schema,
            compare_type=True,
            include_object=lambda obj, name, type_, reflected, compare_to: name
            != "alembic_version",
        )
        with context.begin_transaction():
            context.run_migrations()

    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    offline()
else:
    asyncio.run(online())
