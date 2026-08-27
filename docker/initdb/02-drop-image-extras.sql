-- The postgis/postgis image's own init script (10_postgis.sh) installs
-- postgis_tiger_geocoder, postgis_topology and fuzzystrmatch into POSTGRES_DB
-- and adds their schemas to the DB search_path. We use none of them, and the
-- leftover tables pollute `alembic revision --autogenerate` against the dev DB
-- with bogus DROP statements. Runs only on first init of an empty volume;
-- an already-initialized dev DB was cleaned by hand the same way (2026-08-27).
\c ruxsatnoma
DROP EXTENSION IF EXISTS postgis_tiger_geocoder CASCADE;
DROP EXTENSION IF EXISTS postgis_topology CASCADE;
DROP EXTENSION IF EXISTS fuzzystrmatch;
DROP SCHEMA IF EXISTS tiger CASCADE;
DROP SCHEMA IF EXISTS tiger_data CASCADE;
DROP SCHEMA IF EXISTS topology CASCADE;
ALTER DATABASE ruxsatnoma RESET search_path;
