-- Schema arch: the archive. Specification module 10.13.
--
-- One table, and one deliberate omission that carries most of the design: there
-- are NO foreign keys out of this schema.
--
-- The archive outlives what it archives. A completed violation case, an expired
-- permit or a superseded contour version may be pruned from the operational
-- tables while the archived document must stay readable and provable for its
-- full retention period. A foreign key would either block that pruning or drag
-- the archive row down with it, and both outcomes are wrong. The link is
-- therefore polymorphic and unenforced by design: object_type plus object_id.
--
-- Retention periods come from the records nomenclature of the Agency, and
-- long-term preservation is handed over to the centralised electronic archive
-- under resolution PQ-197.

CREATE TABLE arch.archive_item (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- What was archived. No foreign key on purpose, see the note above.
    object_type          text NOT NULL,
    object_id            uuid NOT NULL,
    -- Entry of the records nomenclature this document falls under. It is the
    -- nomenclature, not the object type, that decides the retention period.
    document_type        text NOT NULL,
    title                text NOT NULL,
    -- Held as plain values rather than references for the same reason: the
    -- archive must survive the reorganisation of an organization that no longer
    -- exists.
    organization_id      uuid,
    territory_code       text,

    -- Where the file physically lives.
    storage_backend      text NOT NULL DEFAULT 'MINIO',
    storage_bucket       text,
    storage_key          text NOT NULL,
    file_name            text NOT NULL,
    mime_type            text NOT NULL,
    -- Long-term preservation format. PDF/A and XML are the archival ones; the
    -- rest are accepted because the source document already exists in them.
    format               text NOT NULL,
    size_bytes           bigint NOT NULL,

    -- Integrity. Module 10.13 requires that an archived document can be proven
    -- unchanged, and the check is a hash comparison against stored bytes.
    hash                 text NOT NULL,
    hash_algorithm       text NOT NULL DEFAULT 'SHA-256',
    integrity_status     text NOT NULL DEFAULT 'UNKNOWN',
    integrity_checked_at  timestamptz,

    -- Retention. retention_until is computed once, when the item is archived,
    -- from the nomenclature in force at that moment. Recomputing it later would
    -- let a change in the nomenclature silently shorten the life of documents
    -- that were filed under the old rules.
    archived_at          timestamptz NOT NULL DEFAULT now(),
    retention_years      int NOT NULL,
    retention_until      date NOT NULL,
    disposition          text NOT NULL DEFAULT 'RETAIN',

    status               text NOT NULL DEFAULT 'STORED',
    restored_at          timestamptz,
    restored_by          uuid,

    -- Handover to the centralised electronic archive, resolution PQ-197.
    central_archive_id   text,
    transferred_at       timestamptz,

    legal_base           text,
    -- Anything the nomenclature entry needs and this table does not model:
    -- series and number of the source document, case index, sheet count.
    metadata             jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           uuid,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    updated_by           uuid,

    -- The same bytes filed twice for the same object are one archive item.
    CONSTRAINT archive_item_object_hash_unique UNIQUE (object_type, object_id, hash),
    CONSTRAINT archive_item_object_type_known CHECK (
        object_type IN (
            'APPLICATION',
            'CONTRACT',
            'CALCULATION',
            'PERMIT',
            'FOREST_TICKET',
            'INVOICE',
            'PAYMENT',
            'REFUND',
            'INSPECTION_ACT',
            'VIOLATION_CASE',
            'MEDIA',
            'REPORT',
            'CONTOUR_VERSION',
            'NORM',
            'SIGNATURE',
            'OTHER'
        )
    ),
    CONSTRAINT archive_item_storage_backend_known CHECK (
        storage_backend IN ('MINIO', 'FILESYSTEM', 'CENTRAL_ARCHIVE')
    ),
    CONSTRAINT archive_item_format_known CHECK (
        format IN (
            'PDF/A', 'PDF', 'XML', 'JSON', 'CSV', 'XLSX', 'DOCX',
            'JPEG', 'PNG', 'TIFF', 'MP4', 'ZIP'
        )
    ),
    CONSTRAINT archive_item_size_positive CHECK (size_bytes > 0),
    CONSTRAINT archive_item_hash_present CHECK (length(hash) > 0),
    CONSTRAINT archive_item_hash_algorithm_known CHECK (
        hash_algorithm IN ('SHA-256', 'SHA-512')
    ),
    CONSTRAINT archive_item_integrity_status_known CHECK (
        integrity_status IN ('UNKNOWN', 'VALID', 'CORRUPTED')
    ),
    CONSTRAINT archive_item_integrity_checked_paired CHECK (
        (integrity_status = 'UNKNOWN') = (integrity_checked_at IS NULL)
    ),
    CONSTRAINT archive_item_retention_years_positive CHECK (retention_years > 0),
    CONSTRAINT archive_item_disposition_known CHECK (
        disposition IN ('RETAIN', 'PERMANENT', 'TRANSFERRED', 'DISPOSED')
    ),
    CONSTRAINT archive_item_status_known CHECK (
        status IN ('STORED', 'RESTORED', 'TRANSFERRED', 'DISPOSED')
    ),
    CONSTRAINT archive_item_transferred_paired CHECK (
        (status = 'TRANSFERRED') = (transferred_at IS NOT NULL)
    ),
    CONSTRAINT archive_item_transferred_has_reference CHECK (
        status <> 'TRANSFERRED' OR central_archive_id IS NOT NULL
    ),
    CONSTRAINT archive_item_metadata_is_object CHECK (
        jsonb_typeof(metadata) = 'object'
    )
);

-- Retrieval by the object that was archived: "show me everything filed for this
-- permit". The most common archive query there is.
CREATE INDEX archive_item_by_object
    ON arch.archive_item (object_type, object_id, archived_at DESC);

-- The scheduled job that walks items whose retention has run out.
CREATE INDEX archive_item_retention_due
    ON arch.archive_item (retention_until)
    WHERE status = 'STORED' AND disposition = 'RETAIN';

-- The scheduled job that re-verifies hashes, oldest check first.
CREATE INDEX archive_item_integrity_sweep
    ON arch.archive_item (integrity_checked_at NULLS FIRST)
    WHERE status IN ('STORED', 'RESTORED');

-- Archive search by organization and territory.
CREATE INDEX archive_item_by_organization
    ON arch.archive_item (organization_id, archived_at DESC)
    WHERE organization_id IS NOT NULL;

-- Search by document title. Trigram rather than full text: there is still no
-- Uzbek dictionary for PostgreSQL, and this handles typos and partial matches.
CREATE INDEX archive_item_title_trgm
    ON arch.archive_item USING gin (title gin_trgm_ops);
