"""PostgreSQL wiring: async engine, session factory, Base, shared column helpers."""

import uuid
from datetime import datetime

import uuid_utils
from sqlalchemy import DateTime, MetaData, Text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names for Alembic migrations
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Project-wide column defaults (decision #27): time is timestamptz (UTC),
    # strings are TEXT (statuses constrained by CHECK, not varchar length).
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        str: Text(),
    }


def uuid7() -> uuid.UUID:
    """UUIDv7 (time-ordered) as a stdlib UUID — the PK default for all tables (decision #27).

    uuid-utils returns its own UUID class; asyncpg/SQLAlchemy expect stdlib uuid.UUID,
    so we re-wrap the bytes.
    """
    return uuid.UUID(bytes=uuid_utils.uuid7().bytes)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
