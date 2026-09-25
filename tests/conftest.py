import asyncio
import hashlib
import io
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.models_registry  # noqa: F401  # populate Base.metadata for migration tests
from app.config import get_settings
from app.core import storage
from app.db import make_engine, make_session_factory

os.environ.setdefault("WORKERS_MODE", "off")  # lifespans in tests must not spawn workers
# `make check` picks tests with pytest-testmon (decision #229), which records what
# each test executes through coverage.py's per-test contexts. On Python 3.14
# coverage defaults to its `sys.monitoring` core, which records a line the FIRST
# time any test runs it and never again — so testmon saw `permits.service.set_status`
# in 2 tests out of the 25 that break without it (measured 2026-09-25) and would
# have skipped the other 23. The C tracer records every test. testmon builds its
# Coverage with `config_file=False`, so only the environment reaches it; this line
# runs before testmon's `pytest_configure`, and `_testmon_records_every_test` below
# refuses a run in which it did not take effect.
os.environ["COVERAGE_CORE"] = "ctrace"


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


@pytest.fixture(scope="session", autouse=True)
def _testmon_records_every_test() -> None:
    """Stop a testmon run whose coverage is not the C tracer (see COVERAGE_CORE
    above): its map would silently under-record, and every later `make check`
    would skip tests it should run. A fixture, not a test — testmon deselects a
    test whose code did not change, and this has to hold on every run."""
    try:
        from testmon.testmon_core import TestmonCollector
    except ImportError:
        return
    if not TestmonCollector.coverage_stack:
        return  # testmon is not collecting in this run
    tracer = TestmonCollector.coverage_stack[-1]._collector.tracer_name()
    if tracer != "CTracer":
        pytest.exit(
            f"testmon is recording through {tracer}, not CTracer: its map would skip "
            "tests. COVERAGE_CORE must be 'ctrace' (tests/conftest.py); delete "
            ".testmondata and run `make check` again.",
            returncode=3,
        )


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


class _SharedEngine(AsyncEngine):
    """An engine whose `dispose()` does nothing, so an app's lifespan cannot close it.

    `create_app()`'s lifespan makes its own engine and disposes it on the way out,
    so every test that drives the API through `make_client(create_app(),
    lifespan=True)` — over half the suite — opened a fresh pool before its first
    request: TCP, SCRAM auth and SQLAlchemy's dialect initialisation, ~45 ms on an
    idle machine and ~0.5 s beside another session's suite (measured 2026-09-25).
    `_one_engine_per_database` hands every such app this engine instead, one per
    database URL, and disposes it for real once, at the end of the session.
    """

    __slots__ = ()

    async def dispose(self, close: bool = True) -> None:
        return None


class _BucketEnsuredOnce:
    """`app.main`'s view of `app.core.storage`: `ensure_bucket` runs once per
    endpoint and bucket, not once per lifespan. Only the lifespan reads storage
    through `app.main`, so `tests/core/test_storage.py` and every other caller
    still reach the real function."""

    def __init__(self) -> None:
        self._done: set[tuple[str, str]] = set()

    async def ensure_bucket(self) -> None:
        s = get_settings()
        key = (s.s3_endpoint, s.s3_bucket)
        if key not in self._done:
            await storage.ensure_bucket()
            self._done.add(key)


@pytest.fixture(scope="session", autouse=True)
async def _one_engine_per_database() -> AsyncIterator[None]:
    """Every app a test builds shares one engine per database URL (see
    `_SharedEngine`). Keyed by URL because the lifespan reads `DATABASE_URL`
    after a test's own monkeypatch, so a test that points it elsewhere still
    gets an engine for THAT database."""
    import app.main

    engines: dict[str, _SharedEngine] = {}

    def shared_engine(url: str) -> AsyncEngine:
        if url not in engines:
            engines[url] = _SharedEngine(make_engine(url).sync_engine)
        return engines[url]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app.main, "make_engine", shared_engine)
        mp.setattr(app.main, "storage", _BucketEnsuredOnce())
        yield
    for eng in engines.values():
        await AsyncEngine.dispose(eng)


@pytest.fixture(autouse=True)
def _permit_pdf_without_weasyprint(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Issuing a permit renders its PDF through WeasyPrint, and ~270 tests issue one
    only to have a permit to read, sign, suspend or revoke — the render was most of
    their setup. Everywhere but a `real_pdf` test, `render_permit` keeps its cheap
    refusals (`_assert_renderable`: a character the fonts cannot draw; `fill`: an
    unfilled placeholder) and skips the typesetting: the bytes are a stand-in that
    starts like a PDF and is unique to the filled layout, so two permits never
    share a hash and one permit always hashes the same.

    What only the real render proves — the one-page and bundled-faces guards, the
    text a reader extracts, the byte-identical re-render — lives in `real_pdf`
    tests: `tests/modules/permits/test_render.py` and `test_issue.py`."""
    if request.node.get_closest_marker("real_pdf"):
        return
    from app.modules.permits import render

    def stand_in(snapshot, layout_html: str, qr_url: str) -> bytes:
        values = {**snapshot, render.QR_FIELD: qr_url}
        render._assert_renderable(values)
        html = render.fill(layout_html, values)
        return (
            b"%PDF-1.7\n% stand-in "
            + hashlib.sha256(html.encode()).hexdigest().encode()
            + b"\n%%EOF\n"
        )

    monkeypatch.setattr(render, "render_permit", stand_in)


@pytest.fixture(autouse=True)
def _reset_ratelimit():
    from app.core import ratelimit

    ratelimit.reset()
    yield


@pytest.fixture(autouse=True)
def _no_sms_quiet_window(request: pytest.FixtureRequest):
    """The nightly SMS quiet window (decision #152) is OFF for the suite.

    Without this, every test that asserts an SMS was delivered fails between
    21:00 and 08:00 Asia/Tashkent — including in CI, whose runners are the one
    place nobody watches the clock. Caught on the integration branch: the same
    file passed at 09:52 and failed at 01:53.

    Patched at the one place that reads it rather than through
    `system_settings`, so a test that clears that table cannot bring the window
    back by accident. `tests/modules/notifications/test_channels.py` covers the
    window itself and opts out with `@pytest.mark.sms_quiet_window`.
    """
    if "sms_quiet_window" in request.keywords:
        yield
        return
    from app.modules.integrations import service as integrations_service

    original = integrations_service.in_quiet_hours
    integrations_service.in_quiet_hours = lambda *args, **kwargs: False
    try:
        yield
    finally:
        integrations_service.in_quiet_hours = original


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
    app,
    *,
    lifespan: bool = False,
    raise_app_exceptions: bool = False,
    client_address: tuple[str, int] | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Общий тестовый HTTP-клиент поверх ASGI-приложения.

    lifespan=True — поднимает app.router.lifespan_context (нужно роутам с БД,
    т.к. ASGITransport сам lifespan не запускает).
    raise_app_exceptions=False (по умолчанию) — ASGITransport не ре-рейзит
    необработанные исключения хендлеров наружу, а отдаёт итоговый HTTP-ответ
    (нужно, чтобы проверять реальный 500-ответ, а не traceback в тесте).

    `client_address` — the ASGI scope's own `(host, port)` client address,
    passed straight to `httpx.ASGITransport` (whose own default is
    `('127.0.0.1', 123)`, kept here when omitted). Stage 5.2 fix round 1's
    IP-threading test (finding 2) is the first caller that needs a
    DISTINGUISHABLE address: `request.client.host` reads directly from this
    tuple, so it is the one way an ASGI-transport test can prove a real
    signer address reaches an adapter rather than some coincidental or
    hardcoded fallback.
    """
    transport = httpx.ASGITransport(
        app=app,
        raise_app_exceptions=raise_app_exceptions,
        client=client_address or ("127.0.0.1", 123),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        if lifespan:
            async with app.router.lifespan_context(app):
                yield client
        else:
            yield client


# --- Register export (`GET …/export.xlsx`, stage 13) helpers -----------------
#
# Every export test file used to carry its own copy of these two, and six
# tests per endpoint on top (2026-09-15: 250 tests for one shared renderer).
# The file-level shape is now ONE "mirrors the list" test per endpoint that
# reads the sheet through `xlsx_rows` and exercises the cap through
# `export_cap`; the shared `lang` refusal is a static check in
# `tests/test_export_routes.py`.


def xlsx_rows(content: bytes) -> tuple[list[Any], list[tuple[Any, ...]]]:
    """`(headers, rows)` of the workbook's active sheet — the header row's
    values and every data row as a tuple, in sheet order."""
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(content)).active
    assert sheet is not None  # a fresh Workbook always has one active sheet
    headers = [cell.value for cell in sheet[1]]
    return headers, list(sheet.iter_rows(min_row=2, values_only=True))


@contextmanager
def export_cap(rows: int) -> Iterator[None]:
    """Lower `register_export_max_rows` to `rows` for the block, at the one
    place every export reads it. Only that key is overridden — the same
    `settings_store.get_int` serves `session_idle_minutes` to every
    authenticated request, and must keep answering the real value."""
    from app.core import settings_store

    original = settings_store.get_int

    async def capped(db: AsyncSession, key: str) -> int:
        if key == "register_export_max_rows":
            return rows
        return await original(db, key)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings_store, "get_int", capped)
        yield


def assert_export_cut(resp: httpx.Response, *, cap: int) -> None:
    """The three headers of `xlsx.xlsx_response` agree with each other and
    with the sheet: `X-Export-Total` is the register's size, `X-Export-Rows`
    what was written, `X-Export-Truncated` whether the two differ. Call it
    on a response made inside `export_cap(cap)`."""
    assert resp.status_code == 200, resp.text
    total = int(resp.headers["x-export-total"])
    assert resp.headers["x-export-rows"] == str(min(total, cap))
    assert resp.headers["x-export-truncated"] == ("true" if total > cap else "false")
    _, rows = xlsx_rows(resp.content)
    assert len(rows) == min(total, cap)
