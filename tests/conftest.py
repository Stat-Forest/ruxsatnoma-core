import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

import app.models_registry  # noqa: F401  # populate Base.metadata for migration tests
from app.config import get_settings
from app.db import make_engine, make_session_factory

os.environ.setdefault("WORKERS_MODE", "off")  # lifespans in tests must not spawn workers


def _use_a_database_of_this_workers_own() -> None:
    """Under `pytest -n N`, give every xdist worker its own test database.

    Serial runs are untouched: with no PYTEST_XDIST_WORKER in the environment
    this returns immediately and the suite uses DATABASE_URL_TEST as written.
    Under xdist, worker `gw3` gets `<that URL>_gw3` — created on demand by
    `_migrated_test_db` below.

    The suite CANNOT share one database across workers. Nothing here rolls a
    test back (the `db` fixture just opens a session), the schema carries
    partial unique indexes and append-only triggers, and
    `test_migrations.py::test_downgrade_upgrade_roundtrip` drops every table in
    the database it runs against — one worker would be wiping the tables
    another is mid-INSERT on. The per-worktree split CLAUDE.md already
    describes is the same fix one level up; this one nests inside it, so two
    worktrees running in parallel still never meet (`..._311` yields
    `..._311_gw0`, not `..._gw0`).

    Both variables move, not just the test one: a fixture that builds the app
    without monkeypatching DATABASE_URL would otherwise reach the DEV database,
    which is shared by every worker and every worktree at once.

    Module level, not a fixture: `Settings` is `lru_cache`d, so the environment
    has to be right before the FIRST `get_settings()` call anywhere. Importing
    `app.config` does not construct it, and the root conftest is imported
    before any test module or package conftest, so this is early enough.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")  # "gw0", "gw1", ...; unset when serial
    if not worker:
        return
    url = make_url(get_settings().database_url_test)
    per_worker = url.set(database=f"{url.database}_{worker}").render_as_string(hide_password=False)
    os.environ["DATABASE_URL_TEST"] = per_worker
    os.environ["DATABASE_URL"] = per_worker
    get_settings.cache_clear()


_use_a_database_of_this_workers_own()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--fresh-db",
        action="store_true",
        help="Drop and re-create this run's test database before migrating it. "
        "`make test` passes it; a single-file debug run normally should not.",
    )


async def _create_test_database(url_str: str, *, fresh: bool) -> None:
    """Create the test database if it is missing; with `fresh`, re-create it.

    A run inherits whatever the previous run left behind — the suite never
    truncates, and its GIS fixtures place RANDOM boxes. Enough leftover
    polygons and a new box lands on an old one: `ERR-GIS-002` from a fixture
    that has nothing to do with geometry, or an overlap sweep that counts a
    permit written days ago. `--fresh-db` buys determinism for the price of a
    migrate-from-scratch (~15s, and every worker pays it in parallel).
    """
    url = make_url(url_str)
    assert url.database and "test" in url.database, (
        f"refusing to touch {url.database!r}: a test database must have 'test' in its name"
    )
    admin_dsn = (
        url.set(drivername="postgresql", database="postgres")
        .render_as_string(hide_password=False)
        .replace("+asyncpg", "")
    )
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval("select 1 from pg_database where datname = $1", url.database)
        if exists and fresh:
            # FORCE (PG 13+) evicts a connection an earlier crashed run may have left.
            await conn.execute(f'DROP DATABASE "{url.database}" WITH (FORCE)')
            exists = None
        if not exists:
            await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Force tests/test_migrations.py to run dead last, whole module.

    Plain alphabetical collection already puts it after every other top-level
    tests/*.py file, but tests/workers/ sorts after "test_..." ('w' > 't') and
    would otherwise collect — and run — later. Its last test wipes the shared
    test DB (downgrade base; every table dropped and re-created), so nothing
    may run after it. list.sort is stable, so this only partitions
    test_migrations.py to the end — relative order elsewhere is unchanged.
    """
    items.sort(key=lambda item: "test_migrations.py" in item.nodeid)


@pytest.fixture(scope="session", autouse=True)
async def _migrated_test_db(request: pytest.FixtureRequest) -> None:
    """Create this run's test DB if needed, then bring it to head.

    Test collection is alphabetical, so tests/modules/... collects before
    tests/test_migrations.py — without this, a test touching a migrated
    table can run before that table exists. Mirrors the upgrade-to-head
    call test_migrations.py makes itself (idempotent, so no conflict).

    Session-scoped and autouse, so under xdist it runs once per worker — which
    is exactly where the worker's own database has to come into existence.
    """
    url = get_settings().database_url_test
    await _create_test_database(url, fresh=bool(request.config.getoption("--fresh-db")))
    cfg = Config("alembic.ini")
    cfg.attributes["sqlalchemy_url"] = url
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture(scope="session")
async def engine():
    eng = make_engine(get_settings().database_url_test)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncIterator[AsyncSession]:
    factory = make_session_factory(engine)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture(autouse=True)
def _reset_ratelimit():
    from app.core import ratelimit

    ratelimit.reset()
    yield


@pytest.fixture(autouse=True)
def _reset_breaker():
    from app.modules.integrations import breaker

    breaker.reset()
    yield


@pytest.fixture(autouse=True)
def _isolate_subscriptions():
    """The bus is process-global; without this, a handler registered by one
    test fires inside another and the failure surfaces three files away. Root
    conftest, not a per-module one (review round 1, finding I1): the bus is
    `app/core/` state, not `applications` state, and every test package that
    subscribes a spy handler to prove its own wiring — `payments`, `permits`,
    not just `applications` — needs the same teardown, the same reason
    `_reset_ratelimit`/`_reset_breaker` above are here rather than in one
    module's own conftest.

    The production subscriptions are registered FIRST, before the snapshot
    (final review I2). Snapshot-and-restore alone strips them: a snapshot taken
    before anything called `register_event_subscriptions()` does not contain
    them, so the teardown removes whatever `create_app()` wired up during the
    test, and the next direct-ORM test — one that never builds an app — runs
    against an EMPTY bus. Once 3.10a subscribes its invoice handler, a test
    asserting "approving twice does not raise a second invoice" would then pass
    because no handler was wired at all. Registering here instead gives every
    test the production wiring, and `register_event_subscriptions()` is
    idempotent by construction (`core.events.subscribe` dedups the
    `(name, handler)` pair), so the repeat calls `create_app()` makes are
    no-ops. The restore still does its original job: a spy subscribed by one
    test is not in the snapshot and is gone by the next.

    **It snapshots `events._SUBSCRIBERS` and NOTHING ELSE.**
    `register_event_subscriptions()` also fills registries that live outside the
    bus — `gis.service.OCCUPANCY_PROVIDERS` and `norms.service.LOAD_PROVIDERS`
    since 3.11a — and this fixture neither snapshots nor restores those. Anything
    registered into such a global must check membership before appending, the way
    `core.events.subscribe` dedups its own `(name, handler)` pairs: a bare
    `.append()` here adds one copy per TEST, so occupancy silently doubles and
    then triples, and the failure reads as pollution in whatever file happens to
    run late rather than as a registration bug — passing whenever that file is
    run alone."""
    from app.core import events
    from app.event_subscriptions import register_event_subscriptions

    register_event_subscriptions()
    saved = {name: list(handlers) for name, handlers in events._SUBSCRIBERS.items()}
    yield
    events._SUBSCRIBERS.clear()
    events._SUBSCRIBERS.update(saved)


@asynccontextmanager
async def make_client(
    app, *, lifespan: bool = False, raise_app_exceptions: bool = False
) -> AsyncIterator[httpx.AsyncClient]:
    """Общий тестовый HTTP-клиент поверх ASGI-приложения.

    lifespan=True — поднимает app.router.lifespan_context (нужно роутам с БД,
    т.к. ASGITransport сам lifespan не запускает).
    raise_app_exceptions=False (по умолчанию) — ASGITransport не ре-рейзит
    необработанные исключения хендлеров наружу, а отдаёт итоговый HTTP-ответ
    (нужно, чтобы проверять реальный 500-ответ, а не traceback в тесте).
    """
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        if lifespan:
            async with app.router.lifespan_context(app):
                yield client
        else:
            yield client
