-- Spatial data: the layer registry, contours with their version history and
-- contour occupancy. Specification module 10.2, scenario S17.
--
-- Applied after 02-schemas.sql, which creates the schema, and after the iam
-- schema, which owns iam.organization. Foreign keys pointing at app.application
-- and permit.permit are declared in 95-constraints.sql: those tables are created
-- later in the sequence, so the columns here stay plain uuid for now.
--
-- Storage SRID is 4326 (WGS 84). It is provisional -- the coordinate system is
-- still an open question with the customer. Areas are computed by casting the
-- geometry to geography, which yields metres without committing to a projection,
-- and stored denormalised in hectares. The geometry stays the source of truth.


-- ---------------------------------------------------------------------------
-- geo.layer -- registry of map layers
-- ---------------------------------------------------------------------------
-- Thirteen layers fixed by specification module 10.2 and scenario S17 step 1.
-- The code is a stable Latin identifier used by the API, by the import routine
-- and by the topology checks; labels shown to a user are resolved from locales/
-- by that code rather than stored here.

CREATE TABLE geo.layer (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code        text NOT NULL UNIQUE,
    name        text NOT NULL,                  -- English label, for operators and documentation
    sort_order  integer NOT NULL,               -- drawing order on the map, ascending
    status      text NOT NULL DEFAULT 'ACTIVE',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid,
    updated_by  uuid,
    -- No deletion anywhere in the system: a layer that falls out of use is
    -- archived instead. Specification clause 4.2.4.
    CONSTRAINT layer_status_known CHECK (status IN ('ACTIVE', 'ARCHIVED'))
);

INSERT INTO geo.layer (code, name, sort_order) VALUES
    ('FOREST_FUND',           'Forest fund',           1),
    ('ORGANIZATION_BOUNDARY', 'Organization boundary', 2),
    ('CONTOUR',               'Contour',               3),
    ('PASTURE',               'Pasture',               4),
    ('HAYFIELD',              'Hayfield',              5),
    ('APIARY',                'Beehive placement',     6),
    ('RECREATION',            'Recreation',            7),
    ('RESTRICTION',           'Restriction',           8),
    ('PROTECTION',            'Protection',            9),
    ('ROTATION',              'Rotation',             10),
    ('REST',                  'Resting area',         11),
    ('WATER_POINT',           'Water point',          12),
    ('CATTLE_ROUTE',          'Cattle route',         13)
ON CONFLICT (code) DO NOTHING;


-- ---------------------------------------------------------------------------
-- geo.contour -- the spatial unit a permit is issued against
-- ---------------------------------------------------------------------------
-- Identity and current state. Provenance of the geometry lives one table down,
-- in geo.contour_version, because permits stay bound to the version they were
-- issued against even after the contour is edited (scenario S17 clause 17.4).

CREATE TABLE geo.contour (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    layer_id           uuid NOT NULL REFERENCES geo.layer,
    organization_id    uuid NOT NULL REFERENCES iam.organization,
    territory_code     text NOT NULL,       -- denormalised from the owning organization, for ABAC filters
    name               text,                -- label shown on the map
    -- Current geometry, denormalised from the current version. The availability
    -- query reads it directly (architecture/database.md, section 4.2) and vector
    -- tiles are built from it, so it must not require a join.
    -- Nullable on purpose: contours migrated from the legacy system arrive
    -- without geometry and sit in status LEGACY until a real outline exists.
    geometry           geometry(MultiPolygon, 4326),
    area_ha            numeric(12,4),
    current_version_id uuid,                -- foreign key added below, once contour_version exists
    approval_doc_id    uuid,                -- document the published version was approved by
    status             text NOT NULL DEFAULT 'DRAFT',
    legacy_id          bigint,
    legacy_table       text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    created_by         uuid,
    updated_by         uuid,
    -- DRAFT through ARCHIVED is the lifecycle of scenario S17 step 5. LEGACY is
    -- ours: a migrated contour that carries history but accepts no application.
    CONSTRAINT contour_status_known CHECK (
        status IN ('DRAFT', 'REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED', 'LEGACY')
    ),
    -- Self-intersecting geometry is rejected by the database, not by the
    -- application. Error ERR-GIS-001.
    CONSTRAINT contour_geometry_valid CHECK (geometry IS NULL OR ST_IsValid(geometry)),
    CONSTRAINT contour_area_not_negative CHECK (area_ha IS NULL OR area_ha >= 0),
    -- Scenario S17 clause 17.3: publishing without the approving document is
    -- blocked. Error ERR-GIS-005.
    CONSTRAINT contour_publish_requires_approval CHECK (
        status <> 'PUBLISHED' OR approval_doc_id IS NOT NULL
    ),
    -- A published contour is selectable in applications, so it must have an
    -- outline. This is what keeps a LEGACY contour from being published as is.
    CONSTRAINT contour_publish_requires_geometry CHECK (
        status <> 'PUBLISHED' OR geometry IS NOT NULL
    )
);


-- ---------------------------------------------------------------------------
-- geo.contour_version -- versioned geometry and its provenance
-- ---------------------------------------------------------------------------
-- Attributes listed in scenario S17 step 4. Editing a contour archives the
-- previous version rather than overwriting it, so a permit issued years ago can
-- still be read against the outline it was issued for.

CREATE TABLE geo.contour_version (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contour_id      uuid NOT NULL REFERENCES geo.contour,
    version_number  integer NOT NULL,
    geometry        geometry(MultiPolygon, 4326),   -- nullable for the same reason as on geo.contour
    area_ha         numeric(12,4),
    source          text,                           -- where the outline came from
    accuracy_m      numeric(8,2),                   -- positional accuracy in metres
    survey_date     date,
    effective       daterange NOT NULL,             -- effective_from and effective_to of the specification
    approval_doc_id uuid,
    status          text NOT NULL DEFAULT 'DRAFT',
    legacy_id       bigint,
    legacy_table    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid,
    updated_by      uuid,
    CONSTRAINT contour_version_unique UNIQUE (contour_id, version_number),
    CONSTRAINT contour_version_number_positive CHECK (version_number > 0),
    CONSTRAINT contour_version_status_known CHECK (
        status IN ('DRAFT', 'REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED', 'LEGACY')
    ),
    CONSTRAINT contour_version_source_known CHECK (
        source IS NULL
        OR source IN ('SURVEY', 'CADASTRE', 'AERIAL', 'GPS', 'IMPORT', 'MANUAL', 'LEGACY')
    ),
    CONSTRAINT contour_version_geometry_valid CHECK (geometry IS NULL OR ST_IsValid(geometry)),
    CONSTRAINT contour_version_area_not_negative CHECK (area_ha IS NULL OR area_ha >= 0),
    CONSTRAINT contour_version_accuracy_not_negative CHECK (accuracy_m IS NULL OR accuracy_m >= 0),
    CONSTRAINT contour_version_publish_requires_approval CHECK (
        status <> 'PUBLISHED' OR approval_doc_id IS NOT NULL
    ),
    CONSTRAINT contour_version_publish_requires_geometry CHECK (
        status <> 'PUBLISHED' OR geometry IS NOT NULL
    ),
    -- At most one version of a contour is published for any given day. Stronger
    -- than a partial unique index: it still allows a replacement version to be
    -- prepared with a future effective period.
    CONSTRAINT contour_version_no_overlapping_published EXCLUDE USING gist (
        contour_id WITH =,
        effective  WITH &&
    ) WHERE (status = 'PUBLISHED')
);

-- Declared after contour_version because the two tables reference each other.
ALTER TABLE geo.contour
    ADD CONSTRAINT contour_current_version_fk
    FOREIGN KEY (current_version_id) REFERENCES geo.contour_version;


-- ---------------------------------------------------------------------------
-- geo.occupancy -- area of a contour taken by an application or a permit
-- ---------------------------------------------------------------------------
-- Definition taken from architecture/database.md, section 5. Reserved when an
-- application moves to SUBMITTED, released on REJECTED, CANCELLED,
-- EXPIRED_UNPAID and when the permit expires.
--
-- application_id and permit_id are plain uuid columns here: app.application and
-- permit.permit do not exist yet at this point in the sequence. Their foreign
-- keys are attached in 95-constraints.sql.

CREATE TABLE geo.occupancy (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contour_id     uuid NOT NULL REFERENCES geo.contour,
    application_id uuid,                     -- foreign key in 95-constraints.sql
    permit_id      uuid,                     -- foreign key in 95-constraints.sql
    geometry       geometry(MultiPolygon, 4326) NOT NULL,
    period         daterange NOT NULL,
    sb_load        numeric(12,2) NOT NULL,   -- load in conditional heads
    status         text NOT NULL,            -- ACTIVE | RELEASED
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid,
    updated_by     uuid,
    CHECK (ST_IsValid(geometry)),
    CONSTRAINT occupancy_status_known CHECK (status IN ('ACTIVE', 'RELEASED')),
    CONSTRAINT occupancy_sb_load_not_negative CHECK (sb_load >= 0),
    CONSTRAINT occupancy_has_owner CHECK (application_id IS NOT NULL OR permit_id IS NOT NULL)
);


-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- One GiST index per geometry column. Specification clause 4.1.4 allows three
-- seconds for a GIS operation, which is not reachable with a sequential scan.
CREATE INDEX contour_geom_gix         ON geo.contour         USING gist (geometry);
CREATE INDEX contour_version_geom_gix ON geo.contour_version USING gist (geometry);
CREATE INDEX occupancy_geom_gix       ON geo.occupancy       USING gist (geometry);

-- The workhorse of the limit check. It has to cover the availability query of
-- architecture/database.md section 4.2 whole: contour, overlapping period,
-- active rows only. Overlap check must finish within five seconds.
-- The uuid column in a GiST index is what btree_gist is installed for.
CREATE INDEX occupancy_lookup ON geo.occupancy USING gist (contour_id, period)
    WHERE status = 'ACTIVE';

-- Releasing an occupancy starts from the application or the permit that holds it.
CREATE INDEX occupancy_by_application ON geo.occupancy (application_id)
    WHERE application_id IS NOT NULL;
CREATE INDEX occupancy_by_permit ON geo.occupancy (permit_id)
    WHERE permit_id IS NOT NULL;

-- Contour listings: by layer for the map, by organization and by territory for
-- the access filter and for the prosecutor search of scenario S22.
CREATE INDEX contour_by_layer        ON geo.contour (layer_id, status);
CREATE INDEX contour_by_organization ON geo.contour (organization_id, status);
CREATE INDEX contour_by_territory    ON geo.contour (territory_code, status);

-- Migration idempotency: rerunning the import must not duplicate a row.
CREATE UNIQUE INDEX contour_legacy_uniq ON geo.contour (legacy_table, legacy_id)
    WHERE legacy_id IS NOT NULL;

-- Version history of a contour, newest first.
CREATE INDEX contour_version_by_contour ON geo.contour_version (contour_id, version_number DESC);
