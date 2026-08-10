-- Schema `permit`: issued permits, versioned document templates, digital
-- signatures and forest tickets. Covers modules 10.6 (permit and document)
-- and 10.7 (digital signature) of the specification.
--
-- Applied after 50-app.sql and before 75-pay.sql: this file references
-- iam, geo and app, and is referenced by pay.
--
-- House rules that hold for every table below:
--   * primary keys are uuid with gen_random_uuid();
--   * every point in time is timestamptz, permit validity is a pair of dates;
--   * money is numeric(18,2), never float and never int;
--   * enumerations are text plus a CHECK list, not enum types: an enum type is
--     painful to change with a migration;
--   * every table carries created_at, updated_at, created_by, updated_by;
--   * tables taking part in the legacy migration also carry legacy_id and
--     legacy_table so any row can be traced back to the old database.


-- ---------------------------------------------------------------------------
-- permit.permit_template
--
-- Printed form of the permit, appendix 1 of the specification. Versioned:
-- a permit is rendered with the template version that was in force when the
-- application was approved, and that version id is kept on the permit
-- (scenario C11, clause 11.3).
-- ---------------------------------------------------------------------------

CREATE TABLE permit.permit_template (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code            text NOT NULL,                     -- stable template code, e.g. PERMIT_GRAZING
    version         text NOT NULL,                     -- template version, referenced by permit.template_id
    activity_type   text NOT NULL,
    title           text NOT NULL,
    language        text NOT NULL DEFAULT 'uz-Cyrl',   -- legal documents are printed in the state language
    layout          jsonb NOT NULL DEFAULT '{}'::jsonb, -- field map and placeholders
    storage_key     text,                              -- template body in object storage
    checksum        text,
    output_format   text NOT NULL DEFAULT 'PDF_A',
    status          text NOT NULL DEFAULT 'DRAFT',
    approval_doc_id uuid,                              -- approving document, required to publish
    effective_from  date NOT NULL,
    effective_to    date,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      uuid,

    CONSTRAINT permit_template_code_version_unique UNIQUE (code, version),

    CONSTRAINT permit_template_activity_type_known CHECK (
        activity_type IN (
            'GRAZING',      -- livestock grazing
            'HAYMAKING',    -- haymaking
            'BEEKEEPING',   -- placement of beehives
            'RECREATION',   -- recreational and cultural use
            'FIREWOOD',     -- collection of firewood, branches and brushwood
            'RESEARCH'      -- scientific research
        )
    ),

    CONSTRAINT permit_template_language_known CHECK (
        language IN ('uz-Cyrl', 'uz-Latn', 'ru', 'en')
    ),

    CONSTRAINT permit_template_output_format_known CHECK (output_format = 'PDF_A'),

    CONSTRAINT permit_template_status_known CHECK (
        status IN ('DRAFT', 'REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED')
    ),

    -- Same rule as for contours and norms: nothing becomes PUBLISHED without
    -- the document that approved it.
    CONSTRAINT permit_template_published_needs_approval CHECK (
        status <> 'PUBLISHED' OR approval_doc_id IS NOT NULL
    ),

    CONSTRAINT permit_template_period_ordered CHECK (
        effective_to IS NULL OR effective_to >= effective_from
    )
);

COMMENT ON TABLE permit.permit_template IS
    'Versioned printed form of a permit. Forms exist for GRAZING and HAYMAKING only; '
    'the other four activity types wait on open question P6.';


-- ---------------------------------------------------------------------------
-- permit.permit_number_a_seq
--
-- Permit numbers are drawn from a sequence per series, never from MAX(number)+1:
-- concurrent issuance would otherwise hand out the same number twice
-- (scenario C11, clause 2). Series A is the first series of appendix 1
-- ("series A no. 000000"); when it is exhausted at 999999 a new sequence is
-- created for the next series.
-- ---------------------------------------------------------------------------

CREATE SEQUENCE permit.permit_number_a_seq
    AS bigint
    START WITH 1
    MINVALUE 1
    MAXVALUE 999999
    NO CYCLE;

COMMENT ON SEQUENCE permit.permit_number_a_seq IS
    'Number generator for permit series A. One sequence per series.';


-- ---------------------------------------------------------------------------
-- permit.permit
--
-- The permit itself, appendix 1 of the specification. Aggregate root: signatures
-- and the forest ticket belong to it. Links to other aggregates are by id only.
-- ---------------------------------------------------------------------------

CREATE TABLE permit.permit (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Origin of the permit.
    application_id      uuid NOT NULL REFERENCES app.application(id),
    contract_id         uuid REFERENCES app.contract(id),   -- shape pending questions P1 and P5
    calculation_id      uuid REFERENCES app.calculation(id),
    template_id         uuid REFERENCES permit.permit_template(id),
    applicant_id        uuid NOT NULL REFERENCES iam.applicant(id),
    organization_id     uuid NOT NULL REFERENCES iam.organization(id),
    territory_code      text NOT NULL,                  -- denormalised for ABAC and RLS

    -- Identity of the document. Uniqueness is guaranteed by the database,
    -- see permit_series_number_unique below.
    series              text NOT NULL,
    number              bigint NOT NULL,                -- from permit.permit_number_<series>_seq
    issued_at           timestamptz NOT NULL DEFAULT now(),

    -- Subject of the permit.
    activity_type       text NOT NULL,
    forestry_code       text,                           -- forest enterprise, from the classifier
    forest_district     text,                           -- forestry district
    patrol_route        text,                           -- patrol route
    block               text,                           -- forest block
    contour_id          uuid REFERENCES geo.contour(id),
    geometry            geometry(MultiPolygon, 4326),
    area_ha             numeric(12,4),                  -- denormalised from geometry for speed
    sb_load             numeric(12,2),                  -- load in conditional heads

    -- Validity. A pair of dates plus a generated range, so both point lookups
    -- and overlap checks are cheap.
    valid_from          date NOT NULL,
    valid_until         date NOT NULL,
    validity            daterange GENERATED ALWAYS AS
                            (daterange(valid_from, valid_until, '[]')) STORED,

    -- Money. numeric(18,2) only: the legacy system rounded through int()
    -- and lost kopecks.
    amount              numeric(18,2) NOT NULL,
    paid_amount         numeric(18,2) NOT NULL DEFAULT 0,
    currency            text NOT NULL DEFAULT 'UZS',
    paid_at             timestamptz,                    -- denormalised from pay, no FK: pay is applied later

    -- Lifecycle, appendix 5 of the specification.
    status              text NOT NULL DEFAULT 'ACTIVE',
    status_reason_code  text,                           -- RJ-01 .. RJ-15
    status_legal_base   text,
    status_changed_at   timestamptz,
    status_document_key text,                           -- decision that changed the status

    -- Immutable signed document and public verification.
    document_key        text,                           -- PDF/A in object storage
    document_hash       text,                           -- hash of the immutable snapshot
    document_sealed_at  timestamptz,                    -- after this moment the document must not change
    qr_token            text,                           -- opaque token behind the QR code
    verification_url    text,

    -- Duplicate (copy) issuance, module 10.6.
    duplicate_of_id     uuid REFERENCES permit.permit(id),
    duplicate_no        int NOT NULL DEFAULT 0,

    legacy_id           bigint,
    legacy_table        text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          uuid,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          uuid,

    CONSTRAINT permit_series_not_blank CHECK (length(btrim(series)) > 0),
    CONSTRAINT permit_number_positive CHECK (number > 0),

    CONSTRAINT permit_activity_type_known CHECK (
        activity_type IN (
            'GRAZING', 'HAYMAKING', 'BEEKEEPING', 'RECREATION', 'FIREWOOD', 'RESEARCH'
        )
    ),

    CONSTRAINT permit_status_known CHECK (
        status IN ('ACTIVE', 'SUSPENDED', 'REVOKED', 'EXPIRED', 'ARCHIVED')
    ),

    CONSTRAINT permit_validity_ordered CHECK (valid_until >= valid_from),

    CONSTRAINT permit_amount_non_negative CHECK (amount >= 0),
    CONSTRAINT permit_paid_amount_non_negative CHECK (paid_amount >= 0),
    CONSTRAINT permit_currency_known CHECK (currency = 'UZS'),

    CONSTRAINT permit_area_non_negative CHECK (area_ha IS NULL OR area_ha >= 0),
    CONSTRAINT permit_sb_load_non_negative CHECK (sb_load IS NULL OR sb_load >= 0),

    -- Every stored geometry is valid, not only the geometry of active permits.
    CONSTRAINT permit_geometry_valid CHECK (geometry IS NULL OR ST_IsValid(geometry)),

    -- A duplicate is a copy of another permit and cannot be a copy of itself.
    CONSTRAINT permit_duplicate_not_self CHECK (duplicate_of_id IS NULL OR duplicate_of_id <> id),
    CONSTRAINT permit_duplicate_no_non_negative CHECK (duplicate_no >= 0),

    -- The signed snapshot is immutable: sealing requires the hash to be present.
    CONSTRAINT permit_sealed_needs_hash CHECK (
        document_sealed_at IS NULL OR (document_key IS NOT NULL AND document_hash IS NOT NULL)
    )
);

COMMENT ON TABLE permit.permit IS
    'Issued permit, appendix 1 of the specification. Aggregate root for signature and forest_ticket.';
COMMENT ON COLUMN permit.permit.number IS
    'Drawn from the sequence of its series. MAX(number)+1 is never used: it duplicates under concurrency.';
COMMENT ON COLUMN permit.permit.template_id IS
    'Template version in force when the application was approved (scenario C11, clause 11.3).';
COMMENT ON COLUMN permit.permit.paid_at IS
    'Denormalised from pay.invoice. No foreign key: schema pay is created after this file.';

-- Series and number of a permit never repeat.
-- Requirement of scenario C11: "uniqueness is guaranteed at the database level".
-- Transferred from architecture/database.md, clause 4.4.
ALTER TABLE permit.permit ADD CONSTRAINT permit_series_number_unique UNIQUE (series, number);

-- An active permit must be bound to GIS.
-- KPI: 100 % of active permits carry a contour_id and a valid geometry.
-- Transferred from architecture/database.md, clause 4.5.
ALTER TABLE permit.permit ADD CONSTRAINT permit_gis_bound CHECK (
    status <> 'ACTIVE' OR (contour_id IS NOT NULL AND geometry IS NOT NULL AND ST_IsValid(geometry))
);

-- Prosecutor filter: permits of a contour by status.
-- Transferred from architecture/database.md, clause 7.
CREATE INDEX permit_by_contour      ON permit.permit (contour_id, status);

CREATE INDEX permit_geom_gix        ON permit.permit USING gist (geometry);
CREATE INDEX permit_by_application  ON permit.permit (application_id);
CREATE INDEX permit_by_applicant    ON permit.permit (applicant_id, issued_at DESC);
CREATE INDEX permit_by_territory    ON permit.permit (territory_code, status, issued_at DESC);
CREATE INDEX permit_expiring        ON permit.permit (valid_until) WHERE status = 'ACTIVE';
CREATE INDEX permit_by_qr_token     ON permit.permit (qr_token) WHERE qr_token IS NOT NULL;
CREATE INDEX permit_by_legacy       ON permit.permit (legacy_table, legacy_id)
    WHERE legacy_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- permit.signature
--
-- Digital signature, module 10.7. A permit form carries up to four signatures:
-- head of the forest enterprise, chief forester, chief accountant and the user.
--
-- OPEN QUESTION P7. The specification does not define whether all four
-- signatures are mandatory, in which order they are applied, what happens when
-- an official is absent, or whether the permit is valid before every signature
-- is collected. Until that is answered no ordering and no mandatory set is
-- encoded here: the table records who signed, with which certificate, when,
-- over which hash and with what verification result. Encoding a guessed order
-- as a constraint would have to be dropped by a migration once the answer
-- arrives, and would silently reject legitimate documents in the meantime.
-- ---------------------------------------------------------------------------

CREATE TABLE permit.signature (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Signed object. The specification allows a permit, a contract or an
    -- application to be signed, so the target is kept both generically
    -- (object_type, object_id) and as a real foreign key.
    object_type             text NOT NULL,
    object_id               uuid NOT NULL,
    permit_id               uuid REFERENCES permit.permit(id),
    contract_id             uuid REFERENCES app.contract(id),
    application_id          uuid REFERENCES app.application(id),

    -- Signer.
    signer_role             text NOT NULL,
    signer_user_id          uuid REFERENCES iam.user_account(id),
    signer_applicant_id     uuid REFERENCES iam.applicant(id),
    signer_name             text NOT NULL,
    signer_pinfl            text,
    signer_tin              text,

    -- Certificate, issued by the State Tax Committee per resolution VMQ 679.
    certificate_serial      text NOT NULL,
    certificate_subject     text,
    certificate_issuer      text,
    certificate_valid_from  timestamptz,
    certificate_valid_until timestamptz,

    -- Signature itself.
    signed_at               timestamptz NOT NULL DEFAULT now(),
    signature_value         text NOT NULL,          -- detached signature, base64
    signature_algorithm     text NOT NULL DEFAULT 'OZDST-1092-2009',
    document_hash           text NOT NULL,          -- hash of the payload that was signed
    hash_algorithm          text NOT NULL DEFAULT 'OZDST-1106-2009',

    -- Verification, including revocation state from CRL or OCSP.
    verification_result     text NOT NULL DEFAULT 'UNKNOWN',
    verification_method     text NOT NULL DEFAULT 'NONE',
    verified_at             timestamptz,
    error_code              text,                   -- ERR-SIGN-001 on a failed attempt

    created_at              timestamptz NOT NULL DEFAULT now(),
    created_by              uuid,
    updated_at              timestamptz NOT NULL DEFAULT now(),
    updated_by              uuid,

    CONSTRAINT signature_object_type_known CHECK (
        object_type IN ('PERMIT', 'CONTRACT', 'APPLICATION')
    ),

    -- The generic pointer and the typed foreign key must agree, otherwise the
    -- foreign key protects nothing. Written as CASE, not as a chain of ORs:
    -- an OR chain where one branch evaluates to NULL is satisfied by default,
    -- so a missing foreign key would slip through.
    CONSTRAINT signature_target_matches_type CHECK (
        CASE object_type
            WHEN 'PERMIT' THEN
                permit_id IS NOT NULL AND permit_id = object_id
                AND contract_id IS NULL AND application_id IS NULL
            WHEN 'CONTRACT' THEN
                contract_id IS NOT NULL AND contract_id = object_id
                AND permit_id IS NULL AND application_id IS NULL
            WHEN 'APPLICATION' THEN
                application_id IS NOT NULL AND application_id = object_id
                AND permit_id IS NULL AND contract_id IS NULL
            ELSE false
        END
    ),

    -- The four signer roles of appendix 1. Neither their order nor their
    -- mandatory status is constrained: open question P7.
    CONSTRAINT signature_signer_role_known CHECK (
        signer_role IN (
            'FOREST_ENTERPRISE_HEAD',  -- head of the forest enterprise
            'CHIEF_FORESTER',          -- chief forester
            'CHIEF_ACCOUNTANT',        -- chief accountant
            'APPLICANT'                -- the user of the forest fund
        )
    ),

    CONSTRAINT signature_signer_identified CHECK (
        signer_user_id IS NOT NULL OR signer_applicant_id IS NOT NULL
    ),

    CONSTRAINT signature_verification_result_known CHECK (
        verification_result IN (
            'VALID', 'INVALID', 'CERTIFICATE_EXPIRED', 'CERTIFICATE_REVOKED', 'UNKNOWN'
        )
    ),

    CONSTRAINT signature_verification_method_known CHECK (
        verification_method IN ('CRL', 'OCSP', 'NONE')
    ),

    CONSTRAINT signature_certificate_period_ordered CHECK (
        certificate_valid_from IS NULL
        OR certificate_valid_until IS NULL
        OR certificate_valid_until >= certificate_valid_from
    )
);

COMMENT ON TABLE permit.signature IS
    'Digital signatures over permits, contracts and applications. Up to four signatures per permit. '
    'Order and mandatory set deliberately not constrained: open question P7.';
COMMENT ON COLUMN permit.signature.document_hash IS
    'Hash of the exact payload that was signed. Together with permit.document_hash it proves the '
    'signed snapshot has not changed.';

CREATE INDEX signature_by_object ON permit.signature (object_type, object_id, signed_at);
CREATE INDEX signature_by_permit ON permit.signature (permit_id, signer_role)
    WHERE permit_id IS NOT NULL;
CREATE INDEX signature_by_signer ON permit.signature (signer_user_id, signed_at DESC)
    WHERE signer_user_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- permit.forest_ticket
--
-- Forest ticket, resolution VMQ 506: a separate document with its own number,
-- validity and restrictions, issued alongside a permit.
-- ---------------------------------------------------------------------------

CREATE TABLE permit.forest_ticket (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  uuid NOT NULL REFERENCES app.application(id),
    permit_id       uuid REFERENCES permit.permit(id),
    organization_id uuid NOT NULL REFERENCES iam.organization(id),
    territory_code  text NOT NULL,

    number          text NOT NULL,
    issued_at       timestamptz NOT NULL DEFAULT now(),

    valid_from      date NOT NULL,
    valid_until     date NOT NULL,
    validity        daterange GENERATED ALWAYS AS
                        (daterange(valid_from, valid_until, '[]')) STORED,

    restrictions    jsonb NOT NULL DEFAULT '[]'::jsonb,  -- restrictions printed on the ticket
    fire_ban_notice text,                                -- fire safety restrictions, VMQ 506

    status          text NOT NULL DEFAULT 'ACTIVE',
    document_key    text,
    document_hash   text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      uuid,

    CONSTRAINT forest_ticket_number_unique UNIQUE (number),

    CONSTRAINT forest_ticket_status_known CHECK (
        status IN ('ACTIVE', 'SUSPENDED', 'REVOKED', 'EXPIRED', 'ARCHIVED')
    ),

    CONSTRAINT forest_ticket_validity_ordered CHECK (valid_until >= valid_from)
);

COMMENT ON TABLE permit.forest_ticket IS
    'Forest ticket issued together with a permit. Restrictions are stored as data, not as columns: '
    'they differ per activity type and per fire safety season.';

CREATE INDEX forest_ticket_by_application ON permit.forest_ticket (application_id);
CREATE INDEX forest_ticket_by_permit      ON permit.forest_ticket (permit_id)
    WHERE permit_id IS NOT NULL;
CREATE INDEX forest_ticket_expiring       ON permit.forest_ticket (valid_until)
    WHERE status = 'ACTIVE';
