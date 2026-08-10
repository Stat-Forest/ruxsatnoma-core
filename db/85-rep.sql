-- Schema rep: report forms, their columns and filled reports.
-- Specification clause 4.2.6, scenario S20, appendices 2 and 3.
--
-- The whole schema is built around one fact: the forms are not settled. Only
-- two of the six activity types have an approved form (grazing, appendix 2, and
-- haymaking, appendix 3); the remaining four appear at the technical design
-- stage and are approved by a separate act of the Agency. This is question P6,
-- still open on 10 August 2026.
--
-- The consequence is a hard rule for this schema: the composition of a report
-- is DATA, never columns. A new form is rows in report_column, not a migration.
-- Clause 4.2.6.4 requires exactly this ("column management") independently.

-- ---------------------------------------------------------------------------
-- Report forms. Clause 4.2.6.1 to 4.2.6.3: created by a department admin, the
-- author may edit it only while it is in the CREATED state, and it becomes
-- usable once approved.
-- ---------------------------------------------------------------------------

CREATE TABLE rep.report_form (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code              text NOT NULL,             -- stable across versions
    version           int  NOT NULL DEFAULT 1,
    name              text NOT NULL,
    form_type         text NOT NULL,             -- ACTIVITY | SUMMARY | AD_HOC
    activity_type     text,                      -- NULL for forms not tied to one activity
    period_type       text NOT NULL,             -- MONTH | QUARTER | HALF_YEAR | YEAR

    -- Reporting deadlines are set per organization and per report type
    -- (clause 4.2.6.5). Held as data because the breakdown is administrative
    -- and changes without any code involved: an object keyed by organization
    -- with the offset or fixed date at which the report falls due.
    due_config        jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Control totals and cross-column relations checked on submission
    -- (scenario S20 step 4). Rules are data so that a new form ships without a
    -- release.
    validation_rules  jsonb NOT NULL DEFAULT '[]'::jsonb,

    status            text NOT NULL DEFAULT 'CREATED',   -- CREATED | APPROVED | ARCHIVED
    -- Only the author may edit, and only while CREATED. Clause 4.2.6.2.
    author_id         uuid REFERENCES iam.user_account(id),
    approved_at       timestamptz,
    approved_by       uuid REFERENCES iam.user_account(id),
    effective_from    date,
    effective_to      date,
    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        uuid,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        uuid,

    CONSTRAINT report_form_code_version_unique UNIQUE (code, version),
    CONSTRAINT report_form_version_positive CHECK (version > 0),
    CONSTRAINT report_form_type_known CHECK (
        form_type IN ('ACTIVITY', 'SUMMARY', 'AD_HOC')
    ),
    CONSTRAINT report_form_activity_known CHECK (
        activity_type IS NULL OR activity_type IN (
            'GRAZING', 'HAYMAKING', 'BEEKEEPING',
            'RECREATION', 'FIREWOOD', 'RESEARCH'
        )
    ),
    CONSTRAINT report_form_period_type_known CHECK (
        period_type IN ('MONTH', 'QUARTER', 'HALF_YEAR', 'YEAR')
    ),
    CONSTRAINT report_form_status_known CHECK (
        status IN ('CREATED', 'APPROVED', 'ARCHIVED')
    ),
    CONSTRAINT report_form_approved_paired CHECK (
        (status = 'CREATED') OR (approved_at IS NOT NULL)
    ),
    CONSTRAINT report_form_due_config_is_object CHECK (
        jsonb_typeof(due_config) = 'object'
    ),
    CONSTRAINT report_form_validation_rules_is_array CHECK (
        jsonb_typeof(validation_rules) = 'array'
    ),
    CONSTRAINT report_form_period_ordered CHECK (
        effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from
    )
);

CREATE INDEX report_form_lookup
    ON rep.report_form (activity_type, period_type)
    WHERE status = 'APPROVED';

-- ---------------------------------------------------------------------------
-- Report columns. Clause 4.2.6.4: columns and their names are created, edited
-- and deleted per report type, and the creation date is kept.
--
-- Of the 28 columns of appendix 2, 24 are derived from the permit and only the
-- inspection result and the two signatures are entered by hand. That is what
-- `source` records, and it is why a report is closer to a view over permits
-- than to a data entry form.
-- ---------------------------------------------------------------------------

CREATE TABLE rep.report_column (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    form_id     uuid NOT NULL REFERENCES rep.report_form(id) ON DELETE CASCADE,
    ordinal     int  NOT NULL,
    code        text NOT NULL,      -- key under which the value sits in report.data
    name        text NOT NULL,      -- canonical title as entered by the admin
    -- Titles in the five first-release locales, keyed by locale tag
    -- (question O4, closed on 10 August 2026: uz-Cyrl, uz-Latn, ru, kaa, en).
    -- Column titles are user data, not interface strings, so they live here
    -- rather than in locales/.
    label       jsonb NOT NULL DEFAULT '{}'::jsonb,
    group_name  text,               -- column groups, e.g. livestock age bands
    data_type   text NOT NULL,      -- TEXT | NUMBER | MONEY | DATE | BOOLEAN | SIGNATURE | REFERENCE
    source      text NOT NULL,      -- AUTO | MANUAL | CALCULATED
    -- Where an AUTO column reads from, or the expression a CALCULATED column
    -- evaluates. Held as text because the catalogue of derivable fields grows
    -- with every new form.
    source_path text,
    is_required boolean NOT NULL DEFAULT false,
    -- Per-type settings: unit, precision, allowed values, display width.
    options     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid,

    CONSTRAINT report_column_code_unique UNIQUE (form_id, code),
    -- Deferrable so that reordering a form's columns is a single UPDATE and not
    -- a dance around transient collisions.
    CONSTRAINT report_column_ordinal_unique UNIQUE (form_id, ordinal)
        DEFERRABLE INITIALLY IMMEDIATE,
    CONSTRAINT report_column_ordinal_positive CHECK (ordinal > 0),
    CONSTRAINT report_column_data_type_known CHECK (
        data_type IN (
            'TEXT', 'NUMBER', 'MONEY', 'DATE', 'BOOLEAN', 'SIGNATURE', 'REFERENCE'
        )
    ),
    CONSTRAINT report_column_source_known CHECK (
        source IN ('AUTO', 'MANUAL', 'CALCULATED')
    ),
    CONSTRAINT report_column_derived_has_path CHECK (
        source = 'MANUAL' OR source_path IS NOT NULL
    ),
    CONSTRAINT report_column_label_is_object CHECK (
        jsonb_typeof(label) = 'object'
    ),
    CONSTRAINT report_column_options_is_object CHECK (
        jsonb_typeof(options) = 'object'
    )
);

CREATE INDEX report_column_by_form
    ON rep.report_column (form_id, ordinal);

-- ---------------------------------------------------------------------------
-- Filled reports. Scenario S20.
--
-- Clause 20.4: a correction found after approval is entered as a NEW VERSION.
-- The history is kept, never overwritten. Versions of the same report share
-- (form_id, organization_id, period) and exactly one of them is current.
-- ---------------------------------------------------------------------------

CREATE TABLE rep.report (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    form_id           uuid NOT NULL REFERENCES rep.report_form(id),
    organization_id   uuid NOT NULL REFERENCES iam.organization(id),
    -- Denormalised, same reason as elsewhere: the prosecutor and the dashboard
    -- filter by territory and must not pay for a join to do it.
    territory_code    text NOT NULL,
    -- Reports are filed for a range of dates, not a range of moments.
    period            daterange NOT NULL,
    version           int NOT NULL DEFAULT 1,
    is_current        boolean NOT NULL DEFAULT true,

    -- Values keyed by report_column.code. The form decides what is inside;
    -- this schema deliberately knows nothing about it.
    data              jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Outcome of the control-total check, kept so that a returned report can
    -- explain itself without recomputing.
    validation_result jsonb NOT NULL DEFAULT '{}'::jsonb,

    status            text NOT NULL DEFAULT 'DRAFT',
    due_at            timestamptz,
    submitted_at      timestamptz,
    submitted_by      uuid REFERENCES iam.user_account(id),
    approved_at       timestamptz,
    approved_by       uuid REFERENCES iam.user_account(id),
    returned_at       timestamptz,
    -- Clause 20.3: a returned report appears in its own list, with the note.
    return_note       text,

    -- The signature record lives in the permit schema and is referenced by
    -- identifier only, so that reporting does not reach into another module.
    signature_id      uuid,
    signed_at         timestamptz,

    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        uuid,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        uuid,

    CONSTRAINT report_version_unique UNIQUE (form_id, organization_id, period, version),
    CONSTRAINT report_version_positive CHECK (version > 0),
    CONSTRAINT report_period_not_empty CHECK (NOT isempty(period)),
    CONSTRAINT report_status_known CHECK (
        status IN ('DRAFT', 'SUBMITTED', 'RETURNED', 'APPROVED', 'ARCHIVED')
    ),
    CONSTRAINT report_submitted_has_moment CHECK (
        status IN ('DRAFT') OR submitted_at IS NOT NULL
    ),
    CONSTRAINT report_approved_paired CHECK (
        (approved_at IS NULL) = (approved_by IS NULL)
    ),
    CONSTRAINT report_returned_has_note CHECK (
        returned_at IS NULL OR return_note IS NOT NULL
    ),
    CONSTRAINT report_data_is_object CHECK (jsonb_typeof(data) = 'object'),
    CONSTRAINT report_validation_result_is_object CHECK (
        jsonb_typeof(validation_result) = 'object'
    )
);

-- Exactly one live version per form, organization and period. Earlier versions
-- stay as rows with is_current = false.
CREATE UNIQUE INDEX report_current_version_unique
    ON rep.report (form_id, organization_id, period)
    WHERE is_current;

-- Cabinet listing, and the "submitted / returned / accepted / draft" lists of
-- clause 4.2.6.8.
CREATE INDEX report_by_organization
    ON rep.report (organization_id, status, period);

-- Overdue list, clause 20.2.
CREATE INDEX report_overdue
    ON rep.report (due_at)
    WHERE status IN ('DRAFT', 'RETURNED');

-- Dashboard roll-up and the prosecutor's territory filter.
CREATE INDEX report_oversight
    ON rep.report (territory_code, status, period);
