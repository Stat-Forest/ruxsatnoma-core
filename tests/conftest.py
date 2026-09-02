import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

import app.models_registry  # noqa: F401  # populate Base.metadata for migration tests
from app.config import get_settings
from app.db import make_engine, make_session_factory

os.environ.setdefault("WORKERS_MODE", "off")  # lifespans in tests must not spawn workers


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
async def _migrated_test_db() -> None:
    """Bring the test DB to head before any test runs.

    Test collection is alphabetical, so tests/modules/... collects before
    tests/test_migrations.py — without this, a test touching a migrated
    table can run before that table exists. Mirrors the upgrade-to-head
    call test_migrations.py makes itself (idempotent, so no conflict).
    """
    cfg = Config("alembic.ini")
    cfg.attributes["sqlalchemy_url"] = get_settings().database_url_test
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
