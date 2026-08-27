"""Прогон миграций на тестовой БД: upgrade head проходит, расширения на месте,
autogenerate не видит расхождений (страж от C1: DROP TABLE spatial_ref_sys)."""

import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from geoalchemy2 import alembic_helpers
from sqlalchemy import text

from app.config import get_settings
from app.db import Base


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.attributes["sqlalchemy_url"] = url
    return cfg


async def test_upgrade_head_installs_extensions(engine):
    url = get_settings().database_url_test
    # env.py (шаблон -t async) сам крутит event loop — из async-теста зовём в отдельном потоке
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT extname FROM pg_extension WHERE extname IN "
                "('postgis','btree_gist','pg_trgm','citext','unaccent')"
            )
        )
        assert {r[0] for r in rows} == {"postgis", "btree_gist", "pg_trgm", "citext", "unaccent"}


async def test_autogenerate_diff_empty(engine):
    # Не полагаемся на порядок тестов — прогоняем upgrade head сами (идемпотентно).
    url = get_settings().database_url_test
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")

    async with engine.connect() as conn:

        def _diff(sync_conn):
            ctx = MigrationContext.configure(
                sync_conn,
                opts={"compare_type": True, "include_object": alembic_helpers.include_object},
            )
            return compare_metadata(ctx, Base.metadata)

        diff = await conn.run_sync(_diff)
    assert diff == []
