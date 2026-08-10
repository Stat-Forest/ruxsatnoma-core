#!/bin/bash
# Database roles. A shell script rather than plain SQL because passwords come
# from the environment and psql cannot read them from a .sql file.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    -- Owner of the core schemas, full DML.
    CREATE ROLE app_core LOGIN PASSWORD '${CORE_PASSWORD}';

    -- Prosecutor access: read-only, enforced by the database rather than by the
    -- application. Specification appendix 6. Grants on tables are applied in
    -- 96-grants.sql, once the tables exist.
    CREATE ROLE oversight_ro LOGIN PASSWORD '${OVERSIGHT_RO_PASSWORD}';

    -- Migration scripts, used only during the cutover window.
    CREATE ROLE migrator LOGIN PASSWORD '${MIGRATOR_PASSWORD}';
SQL
