"""Shared test fixtures.

The test database is a real PostgreSQL with PostGIS, started in a container
for the whole session and migrated to head. SQLite is used nowhere: it knows
neither PostGIS, nor exclusion constraints, nor row level security, which is
exactly what the invariants of this system rest on.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

# The official postgis/postgis image has no arm64 build for PostgreSQL 18.
POSTGIS_IMAGE = "imresamu/postgis:18-3.6"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _apply_migrations(url: str) -> None:
    """Bring a freshly started database up to head."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    # ConfigParser reads "%" as interpolation and passwords may contain it.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Start PostGIS, migrate it, and hand out the connection URL."""
    with PostgresContainer(POSTGIS_IMAGE, driver="asyncpg") as container:
        url: str = container.get_connection_url()
        _apply_migrations(url)
        yield url
