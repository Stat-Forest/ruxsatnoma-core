-- Schema `iam`: users, roles, permissions, organizations, applicants,
-- delegated powers and sessions.
--
-- Sources: specification clause 4.2.13 and appendix 4 (the ten roles and the
-- permission matrix), appendix 6 (the read-only prosecutor role), scenarios
-- S1 (sign-in), S2 (registration of an individual or a legal entity) and S23
-- (administration).
--
-- Two rules shape most of what follows:
--   * Nothing is deleted. Accounts, organizations and applicants move to
--     status ARCHIVED (scenario S23, steps 23.1-23.3).
--   * Every permission is evaluated inside the territory and organization of
--     the user (ABAC). `territory_code` is therefore carried on the account
--     itself and copied into the session at sign-in, and is the column the
--     row-level security policies of the prosecutor role filter on.

-- ---------------------------------------------------------------------------
-- Organizations: 84 of them per the specification, arranged as a tree through
-- parent_id -- the Agency at the root, territorial departments below it, state
-- forestries and forest districts under those. The old system had 90 rows in
-- `department`; the difference is the reorganisation, see gap B5.
-- ---------------------------------------------------------------------------
CREATE TABLE iam.organization (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Code from the state forestry register. Stable, printed on permits.
    code            text NOT NULL UNIQUE,
    name            text NOT NULL,
    short_name      text,

    -- Mandatory: assignment of an application to an executing organization is
    -- driven by type together with territory (scenario S4, step 1).
    -- HUNTING_DEPARTMENT exists in the old system and depends on question O9.
    type            text NOT NULL
                    CHECK (type IN ('AGENCY',
                                    'TERRITORIAL_DEPARTMENT',
                                    'FORESTRY',
                                    'FOREST_DISTRICT',
                                    'HUNTING_DEPARTMENT')),

    parent_id       uuid REFERENCES iam.organization (id),

    -- Mandatory. Drives ABAC, assignment and the prosecutor's RLS policies.
    territory_code  text NOT NULL,
    -- How far the organization's authority reaches. The Agency is REPUBLIC,
    -- a territorial department REGION, a forestry DISTRICT.
    territory_scope text NOT NULL DEFAULT 'DISTRICT'
                    CHECK (territory_scope IN ('REPUBLIC', 'REGION', 'DISTRICT')),

    tin             text,

    -- Settlement details, migrated from `department_account`. Needed by the
    -- payment module to split an incoming payment between recipients.
    bank_account    text,
    bank_name       text,
    bank_mfo        text,

    address         text,
    phone           text,
    email           text,

    status          text NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')),

    legacy_id       bigint,
    legacy_table    text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid,
    updated_by      uuid,

    CONSTRAINT organization_not_own_parent CHECK (parent_id IS DISTINCT FROM id),
    CONSTRAINT organization_tin_format CHECK (tin IS NULL OR tin ~ '^[0-9]{9}$'),
    CONSTRAINT organization_mfo_format
        CHECK (bank_mfo IS NULL OR bank_mfo ~ '^[0-9]{5}$'),
    CONSTRAINT organization_legacy_pair
        CHECK ((legacy_id IS NULL) = (legacy_table IS NULL))
);

-- Trigram search over names. Stands in for morphological full-text search
-- until an Uzbek dictionary exists. Specification clause 4.2.3.
CREATE INDEX organization_name_trgm ON iam.organization USING gin (name gin_trgm_ops);

CREATE INDEX organization_by_parent
    ON iam.organization (parent_id)
    WHERE parent_id IS NOT NULL;

CREATE INDEX organization_by_territory ON iam.organization (territory_code, status);

CREATE UNIQUE INDEX organization_by_legacy
    ON iam.organization (legacy_table, legacy_id)
    WHERE legacy_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Roles. Ten come with the system; the administrator may create, copy and edit
-- further ones (scenario S23, step 6), so `code` is not constrained to a fixed
-- list. `is_system` protects the ten built-ins from being edited away.
-- ---------------------------------------------------------------------------
CREATE TABLE iam.role (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- SYSTEM_ADMIN, CENTRAL_OFFICER, MANAGEMENT, EXECUTOR,
    -- GIS_NORM_SPECIALIST, ORGANIZATION_HEAD, INSPECTOR, ACCOUNTANT,
    -- APPLICANT, PROSECUTOR.
    code                 text NOT NULL UNIQUE,
    name_uz              text NOT NULL,
    name_ru              text,
    name_en              text,
    description          text,

    is_system            boolean NOT NULL DEFAULT false,

    -- Appendix 6, technical enforcement level three: the prosecutor role is
    -- read-only in the database as well as in the API. A session opened under
    -- a read-only role is routed to the oversight_ro connection pool.
    is_read_only         boolean NOT NULL DEFAULT false,

    -- Which unit and position a user must hold for this role to be offered at
    -- all (scenario S23, step 2: a role that does not fit is not shown).
    assignment_criteria  jsonb NOT NULL DEFAULT '{}'::jsonb,

    status               text NOT NULL DEFAULT 'ACTIVE'
                         CHECK (status IN ('ACTIVE', 'ARCHIVED')),

    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    created_by           uuid,
    updated_by           uuid
);

-- ---------------------------------------------------------------------------
-- Permissions: the catalogue of object-action pairs from appendix 4, seventeen
-- objects by six actions. Fixed data, edited only when the specification is
-- amended. Scenario S23, step 6 counts unassigned functions, which is why the
-- catalogue is a table rather than a constant in code.
-- ---------------------------------------------------------------------------
CREATE TABLE iam.permission (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Composed as OBJECT_TYPE.ACTION, e.g. APPLICATION.APPROVE.
    code        text NOT NULL UNIQUE,

    object_type text NOT NULL
                CHECK (object_type IN ('APPLICATION',
                                       'CALCULATION',
                                       'CONTOUR',
                                       'NORM',
                                       'PERMIT',
                                       'PAYMENT',
                                       'REFUND',
                                       'INSPECTION_ACT',
                                       'VIOLATION_CASE',
                                       'REPORT',
                                       'DASHBOARD',
                                       'USER_AND_ROLE',
                                       'CLASSIFIER',
                                       'AUDIT_LOG',
                                       'SYSTEM_SETTINGS',
                                       'BACKUP',
                                       'ARCHIVE')),

    -- The six marks of appendix 4: view, create, update, approve or sign,
    -- delete, export.
    action      text NOT NULL
                CHECK (action IN ('VIEW', 'CREATE', 'UPDATE',
                                  'APPROVE', 'DELETE', 'EXPORT')),

    name_uz     text NOT NULL,
    name_ru     text,
    name_en     text,
    description text,

    status      text NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE', 'ARCHIVED')),

    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid,
    updated_by  uuid,

    CONSTRAINT permission_object_action_unique UNIQUE (object_type, action)
);

-- ---------------------------------------------------------------------------
-- Which permissions a role holds, and how wide each one reaches. The width is
-- the ABAC half of the matrix: the applicant sees only their own records, an
-- executor their organization, management the whole republic.
-- ---------------------------------------------------------------------------
CREATE TABLE iam.role_permission (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id       uuid NOT NULL REFERENCES iam.role (id),
    permission_id uuid NOT NULL REFERENCES iam.permission (id),

    scope         text NOT NULL DEFAULT 'ORGANIZATION'
                  CHECK (scope IN ('OWN', 'ORGANIZATION', 'TERRITORY', 'REPUBLIC')),

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    created_by    uuid,
    updated_by    uuid,

    CONSTRAINT role_permission_unique UNIQUE (role_id, permission_id)
);

CREATE INDEX role_permission_by_permission ON iam.role_permission (permission_id);

-- ---------------------------------------------------------------------------
-- User accounts. Covers everyone: staff of the Agency and its organizations,
-- inspectors, accountants, prosecutors and the individuals and legal entities
-- who apply through the public portal or my.gov.uz.
--
-- Passwords are not migrated from the old system: the Django hashes are
-- incompatible with Argon2id, and under the new model applicants have no
-- password at all -- only OneID or E-IMZO.
-- ---------------------------------------------------------------------------
CREATE TABLE iam.user_account (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Sign-in name. Scenario S1: an email address for internal staff.
    username                text NOT NULL UNIQUE,
    email                   text,
    phone                   text,
    -- Confirmed by OTP during registration (scenario S2, step 6).
    email_verified_at       timestamptz,
    phone_verified_at       timestamptz,

    -- Personal identification number of an individual (PINFL / JSHSHIR).
    pinfl                   text,
    full_name               text NOT NULL,
    position                text,

    -- Empty for external users: an applicant belongs to no organization.
    organization_id         uuid REFERENCES iam.organization (id),
    role_id                 uuid NOT NULL REFERENCES iam.role (id),

    -- ABAC and RLS. For staff it comes from the organization, for applicants
    -- from their registered address.
    territory_code          text NOT NULL,
    territory_scope         text NOT NULL DEFAULT 'DISTRICT'
                            CHECK (territory_scope IN ('REPUBLIC', 'REGION', 'DISTRICT')),

    -- Argon2id. Empty where password sign-in is not allowed: applicants, and
    -- the prosecutor role, for which clause 4.1.6 forbids it outright.
    password_hash           text,
    password_changed_at     timestamptz,
    -- The administrator issues a one-time password; it must be replaced on
    -- first sign-in (scenario S23, step 4).
    must_change_password    boolean NOT NULL DEFAULT false,

    -- Mandatory for internal staff signing in with a password (scenario S1,
    -- step 4). The secret is encrypted by the application before it is stored.
    mfa_enabled             boolean NOT NULL DEFAULT false,
    mfa_secret              text,

    oneid_subject           text,
    eimzo_certificate_serial text,

    failed_login_count      integer NOT NULL DEFAULT 0,
    -- Set when the attempt limit is exceeded: ERR-AUTH-003.
    locked_until            timestamptz,
    last_login_at           timestamptz,

    -- End of the user's mandate. Sign-in is refused past this date (S1, 1.4);
    -- prosecutor accounts are suspended automatically when their mandate in
    -- "Raqamli nazorat" runs out.
    authority_valid_until   date,

    status                  text NOT NULL DEFAULT 'PENDING'
                            CHECK (status IN ('PENDING', 'ACTIVE', 'SUSPENDED', 'ARCHIVED')),

    legacy_id               bigint,
    legacy_table            text,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    created_by              uuid,
    updated_by              uuid,

    CONSTRAINT user_account_pinfl_format
        CHECK (pinfl IS NULL OR pinfl ~ '^[0-9]{14}$'),
    CONSTRAINT user_account_failed_login_count_non_negative
        CHECK (failed_login_count >= 0),
    CONSTRAINT user_account_legacy_pair
        CHECK ((legacy_id IS NULL) = (legacy_table IS NULL))
);

-- One account per person. Scenario S2, 2.5: a repeat registration under a
-- PINFL that already exists is redirected to sign-in.
CREATE UNIQUE INDEX user_account_by_pinfl
    ON iam.user_account (pinfl)
    WHERE pinfl IS NOT NULL;

CREATE UNIQUE INDEX user_account_by_oneid
    ON iam.user_account (oneid_subject)
    WHERE oneid_subject IS NOT NULL;

-- Staff listing inside an organization, and the monthly review of rights
-- (scenario S23, step 9).
CREATE INDEX user_account_by_organization
    ON iam.user_account (organization_id, status)
    WHERE organization_id IS NOT NULL;

CREATE INDEX user_account_by_territory ON iam.user_account (territory_code, status);

CREATE UNIQUE INDEX user_account_by_legacy
    ON iam.user_account (legacy_table, legacy_id)
    WHERE legacy_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Applicants: the individual or legal entity an application is filed for.
-- Separate from the account because one account may act for a legal entity
-- under a power of attorney, and because a legal entity's details outlive the
-- person who registered it.
-- ---------------------------------------------------------------------------
CREATE TABLE iam.applicant (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    user_account_id     uuid REFERENCES iam.user_account (id),

    type                text NOT NULL CHECK (type IN ('INDIVIDUAL', 'LEGAL_ENTITY')),

    -- Personal identification number of an individual (PINFL / JSHSHIR),
    -- 14 digits. Taxpayer identification number (TIN / STIR), 9 digits.
    pinfl               text,
    tin                 text,

    -- Full name of the individual, or the registered name of the legal entity.
    -- Printed on the permit and searched by the prosecutor (scenario S22).
    full_name           text NOT NULL,

    -- Filled from OneID and the State Personalisation Centre (S2, step 3).
    birth_date          date,
    birth_place         text,
    citizenship_code    text,
    passport_series     text,
    passport_number     text,
    passport_issued_by  text,
    passport_issued_at  date,

    -- Legal entity only: taken from the register of legal entities.
    director_name       text,
    legal_address       text,

    -- ABAC and RLS, and the territory an application defaults to.
    territory_code      text NOT NULL,
    address             text,
    phone               text,
    email               text,

    -- Needed to pay a refund back (module 10.9).
    bank_account        text,
    bank_mfo            text,

    -- Reduced or waived charge under resolution VMQ 278, clauses 9-11.
    benefit_category_id uuid REFERENCES nsi.classifier_value (id),

    -- S2, 2.4: when an external register does not answer, the data is typed in
    -- by hand and the automatic check is queued, so the source is recorded.
    verification_source text
                        CHECK (verification_source IS NULL
                               OR verification_source IN ('ONEID',
                                                          'PERSONALISATION_CENTRE',
                                                          'LEGAL_ENTITY_REGISTRY',
                                                          'MANUAL')),
    verified_at         timestamptz,

    status              text NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')),

    legacy_id           bigint,
    legacy_table        text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    created_by          uuid,
    updated_by          uuid,

    CONSTRAINT applicant_pinfl_format
        CHECK (pinfl IS NULL OR pinfl ~ '^[0-9]{14}$'),
    CONSTRAINT applicant_tin_format
        CHECK (tin IS NULL OR tin ~ '^[0-9]{9}$'),
    -- An individual is identified by PINFL, a legal entity by TIN. A sole
    -- trader may carry both, which is why this is not an exclusive choice.
    CONSTRAINT applicant_identifier_present
        CHECK ((type = 'INDIVIDUAL' AND pinfl IS NOT NULL)
               OR (type = 'LEGAL_ENTITY' AND tin IS NOT NULL)),
    CONSTRAINT applicant_legacy_pair
        CHECK ((legacy_id IS NULL) = (legacy_table IS NULL))
);

-- Prosecutor filters, scenario S22: search by PINFL or TIN within 3 seconds.
CREATE INDEX applicant_by_pinfl ON iam.applicant (pinfl);
CREATE INDEX applicant_by_tin   ON iam.applicant (tin);

-- Trigram search over names. Specification clause 4.2.3.
CREATE INDEX applicant_name_trgm ON iam.applicant USING gin (full_name gin_trgm_ops);

CREATE INDEX applicant_by_user
    ON iam.applicant (user_account_id)
    WHERE user_account_id IS NOT NULL;

CREATE INDEX applicant_by_territory ON iam.applicant (territory_code, status);

CREATE UNIQUE INDEX applicant_by_legacy
    ON iam.applicant (legacy_table, legacy_id)
    WHERE legacy_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Delegated powers. Two cases, one table:
--   REPRESENTATION  -- a person acts for a legal entity under a power of
--                      attorney with a stated term (scenario S2, step 5).
--   DUTY_HANDOVER   -- an employee's open work is passed to a colleague before
--                      the account is suspended (scenario S23, step 5, 23.2).
-- Expiry of the term withdraws the rights on its own: the period is the only
-- thing the permission check reads.
-- ---------------------------------------------------------------------------
CREATE TABLE iam.delegation (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    type               text NOT NULL
                       CHECK (type IN ('REPRESENTATION', 'DUTY_HANDOVER')),

    -- The legal entity being represented.
    applicant_id       uuid REFERENCES iam.applicant (id),
    -- The employee handing the work over.
    delegator_user_id  uuid REFERENCES iam.user_account (id),
    -- Who receives the powers. Always an account.
    delegate_user_id   uuid NOT NULL REFERENCES iam.user_account (id),

    organization_id    uuid REFERENCES iam.organization (id),
    -- Role the delegate acts under while the delegation holds.
    role_id            uuid REFERENCES iam.role (id),
    -- Narrows the delegation further: single activity type, single contour,
    -- named applications.
    scope              jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Term of the mandate. Bounded on both sides: an open-ended power of
    -- attorney is not accepted.
    period             daterange NOT NULL,

    -- The scanned power of attorney in object storage, plus its own details.
    document_file_id   uuid,
    document_number    text,
    document_issued_at date,

    status             text NOT NULL DEFAULT 'ACTIVE'
                       CHECK (status IN ('ACTIVE', 'REVOKED', 'EXPIRED', 'ARCHIVED')),
    revoked_at         timestamptz,
    revoke_reason      text,

    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    created_by         uuid,
    updated_by         uuid,

    CONSTRAINT delegation_period_bounded
        CHECK (NOT isempty(period)
               AND lower(period) IS NOT NULL
               AND upper(period) IS NOT NULL),
    CONSTRAINT delegation_source_present
        CHECK ((type = 'REPRESENTATION' AND applicant_id IS NOT NULL)
               OR (type = 'DUTY_HANDOVER' AND delegator_user_id IS NOT NULL)),
    CONSTRAINT delegation_not_self
        CHECK (delegator_user_id IS DISTINCT FROM delegate_user_id),

    -- One live power of attorney per representative and legal entity at a
    -- time. Overlapping mandates would make it impossible to say which one an
    -- application was filed under.
    CONSTRAINT delegation_no_active_overlap
        EXCLUDE USING gist (applicant_id WITH =,
                            delegate_user_id WITH =,
                            period WITH &&)
        WHERE (status = 'ACTIVE' AND type = 'REPRESENTATION')
);

CREATE INDEX delegation_by_delegate
    ON iam.delegation (delegate_user_id, status);

CREATE INDEX delegation_by_applicant
    ON iam.delegation (applicant_id)
    WHERE applicant_id IS NOT NULL;

-- Scheduled job that expires mandates whose term has run out.
CREATE INDEX delegation_active_period
    ON iam.delegation USING gist (period)
    WHERE status = 'ACTIVE';

-- ---------------------------------------------------------------------------
-- Sessions. Scenario S1, step 6: on sign-in the system resolves the role, the
-- organization and the territory once and puts them in the session token; the
-- resolved values are stored here so that a revoked role or an expired mandate
-- takes effect without waiting for the token to lapse.
--
-- Raw tokens are never stored, only their hashes.
-- ---------------------------------------------------------------------------
CREATE TABLE iam.session (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_account_id     uuid NOT NULL REFERENCES iam.user_account (id),

    token_hash          text NOT NULL UNIQUE,
    refresh_token_hash  text UNIQUE,

    auth_method         text NOT NULL
                        CHECK (auth_method IN ('PASSWORD',
                                               'PASSWORD_MFA',
                                               'ONEID',
                                               'EIMZO')),

    -- Resolved at sign-in and never recomputed within the session.
    role_id             uuid NOT NULL REFERENCES iam.role (id),
    organization_id     uuid REFERENCES iam.organization (id),
    territory_code      text,
    territory_scope     text
                        CHECK (territory_scope IS NULL
                               OR territory_scope IN ('REPUBLIC', 'REGION', 'DISTRICT')),

    -- Set for the prosecutor role. Requests on such a session are routed to
    -- the oversight_ro connection pool and to the GET-only middleware.
    is_read_only        boolean NOT NULL DEFAULT false,

    -- Clause 4.1.6: the prosecutor's mandate is verified live in "Raqamli
    -- nazorat" on every sign-in and the answer is not cached, so the reference
    -- belongs to the session rather than to the account.
    oversight_verified_at timestamptz,
    oversight_reference   text,

    ip                  inet,
    user_agent          text,
    device              text,

    -- Session lifetime. No longer than 30 minutes for the prosecutor role.
    expires_at          timestamptz NOT NULL,
    last_seen_at        timestamptz,
    ended_at            timestamptz,
    end_reason          text
                        CHECK (end_reason IS NULL
                               OR end_reason IN ('LOGOUT',
                                                 'IDLE_TIMEOUT',
                                                 'EXPIRED',
                                                 'REVOKED',
                                                 'ROLE_CHANGED')),

    status              text NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE', 'ENDED', 'ARCHIVED')),

    correlation_id      uuid,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    created_by          uuid,
    updated_by          uuid,

    CONSTRAINT session_ended_has_reason
        CHECK (status <> 'ENDED' OR (ended_at IS NOT NULL AND end_reason IS NOT NULL))
);

-- Listing and revoking a user's live sessions.
CREATE INDEX session_by_user
    ON iam.session (user_account_id, created_at DESC);

-- Scheduled job that closes sessions past their lifetime (scenario S1, 1.5).
CREATE INDEX session_active_expiry
    ON iam.session (expires_at)
    WHERE status = 'ACTIVE';
