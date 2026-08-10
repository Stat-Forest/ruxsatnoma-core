-- Extensions required by the schema.
-- Order matters: everything below depends on these being present.

-- Spatial types, indexes and functions. Specification clause 4.2.5.
CREATE EXTENSION IF NOT EXISTS postgis;

-- Lets scalar types take part in GiST exclusion constraints.
-- Required by app.application_no_active_duplicate, which mixes uuid equality
-- with daterange overlap in a single EXCLUDE. Specification module 10.1.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Trigram search over names. Stands in for morphological full-text search
-- until an Uzbek dictionary exists. Specification clause 4.2.3.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- gen_random_uuid() for primary keys.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
