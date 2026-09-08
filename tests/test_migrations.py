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


async def test_downgrade_upgrade_roundtrip(engine):
    """upgrade head → downgrade base → upgrade head (plan 03.4 ruling 16).

    KEEP THIS TEST LAST IN THIS FILE (this file's ordering is guaranteed by the
    collection hook in tests/conftest.py, not by alphabetical order):
    downgrade base wipes the shared test DB — every table is dropped and
    re-created; data other tests created is gone. Nothing may run after it."""
    url = get_settings().database_url_test
    cfg = _alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    # The head is pinned as a literal on purpose: a second head is invisible to
    # every other test (conftest's migrator is session-scoped and autouse, so a
    # branch point kills the whole suite rather than one case). Move this in the
    # SAME commit as the migration that moves the head.
    assert version == "merge_0039_0042"
