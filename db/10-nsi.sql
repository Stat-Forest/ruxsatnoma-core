-- Schema `nsi`: classifiers and reference data.
--
-- Specification clause 4.1.10 lists fourteen registers: livestock species and
-- age groups, the six activity types, regions, districts, state forestries,
-- forest districts, GIS layers, document types, rejection reasons RJ-01 to
-- RJ-15, violation types, benefit categories, ISO-3166-1 countries and others.
--
-- Every other module reads from here, so this file is applied first. Two
-- tables rather than one: the register is described once, its values are
-- versioned rows underneath it.
--
-- Nothing is ever deleted. A value already referenced by live records moves to
-- status ARCHIVED instead of being removed (scenario S23, step 23.3).

-- ---------------------------------------------------------------------------
-- The register itself: what kind of reference data this is and where it comes
-- from. Roughly fourteen rows on day one, growing as new registers appear.
-- ---------------------------------------------------------------------------
CREATE TABLE nsi.classifier (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Machine name used by application code: activity_type, livestock_species,
    -- region, district, forestry, document_type, reject_reason, country.
    code              text NOT NULL UNIQUE,
    name_uz           text NOT NULL,
    name_ru           text,
    name_en           text,
    description       text,

    -- OWN registers are maintained by the system administrator (scenario S23,
    -- step 8). CS_EGOV registers are synchronised from cs.egov.uz by a
    -- scheduled job in the integration service. ISO covers ISO-3166-1.
    -- LEGAL_ACT marks registers whose values come from a resolution and may
    -- not be edited outside an amendment.
    source            text NOT NULL DEFAULT 'OWN'
                      CHECK (source IN ('OWN', 'CS_EGOV', 'ISO', 'LEGAL_ACT')),
    external_registry text,

    -- Territory registers are trees: region -> district -> forestry ->
    -- forest district -> compartment. Flat registers leave parent_id empty.
    is_hierarchical   boolean NOT NULL DEFAULT false,

    status            text NOT NULL DEFAULT 'ACTIVE'
                      CHECK (status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')),

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    created_by        uuid,
    updated_by        uuid
);

-- ---------------------------------------------------------------------------
-- The values of a register. Versioned by effective_from / effective_to so that
-- classifiers.list(type, on_date) can answer what was in force on any past
-- date -- the same requirement that governs norms and tariffs (clause 4.2.15).
-- ---------------------------------------------------------------------------
CREATE TABLE nsi.classifier_value (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    classifier_id  uuid NOT NULL REFERENCES nsi.classifier (id),

    -- Our own stable identifier inside the register: GRAZING, HAYMAKING,
    -- RJ-04, CATTLE_ADULT. Referenced from permits and calculations, therefore
    -- never rewritten once published.
    code           text NOT NULL,
    name_uz        text NOT NULL,
    name_ru        text,
    name_en        text,

    parent_id      uuid REFERENCES nsi.classifier_value (id),

    -- The same value's code in the source registry: SOATO for cs.egov.uz,
    -- alpha-2 for ISO-3166-1. Kept apart from `code` so that a renumbering on
    -- the far side never touches our identifiers.
    external_code  text,
    sort_order     integer NOT NULL DEFAULT 0,

    -- Register-specific payload: conditional head coefficients for livestock
    -- species (resolution VMQ 689, appendix 5), legal basis for rejection
    -- reasons, SRID for GIS layers. Structured columns are added here only
    -- once a register needs to be queried by that field.
    attributes     jsonb NOT NULL DEFAULT '{}'::jsonb,

    effective_from date NOT NULL DEFAULT CURRENT_DATE,
    effective_to   date,

    status         text NOT NULL DEFAULT 'ACTIVE'
                   CHECK (status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')),

    -- Legacy `region` and `application_holidays` are migrated into this table.
    -- Origin is kept so any row can be traced back to the old database.
    legacy_id      bigint,
    legacy_table   text,

    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid,
    updated_by     uuid,

    CONSTRAINT classifier_value_code_unique UNIQUE (classifier_id, code),
    CONSTRAINT classifier_value_not_own_parent CHECK (parent_id IS DISTINCT FROM id),
    CONSTRAINT classifier_value_period_ordered
        CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CONSTRAINT classifier_value_legacy_pair
        CHECK ((legacy_id IS NULL) = (legacy_table IS NULL))
);

-- Listing a register in display order -- the query behind every dropdown.
CREATE INDEX classifier_value_by_classifier
    ON nsi.classifier_value (classifier_id, status, sort_order);

-- Walking the territory tree: region -> district -> forestry.
CREATE INDEX classifier_value_by_parent
    ON nsi.classifier_value (parent_id)
    WHERE parent_id IS NOT NULL;

-- classifiers.list(type, on_date): what was in force on a given date.
CREATE INDEX classifier_value_effective
    ON nsi.classifier_value (classifier_id, effective_from, effective_to);

-- Migration scripts must be re-runnable without producing duplicates; they
-- match on origin, so the pair has to be unique.
CREATE UNIQUE INDEX classifier_value_by_legacy
    ON nsi.classifier_value (legacy_table, legacy_id)
    WHERE legacy_id IS NOT NULL;
