-- Schema insp: field inspection, electronic acts, evidential media, violation
-- cases. Specification module 10.8, scenarios S6, S15 and S16.
--
-- Three things drive the shape of this schema and are easy to lose later:
--
--   * An act is drawn up in the field, frequently with no connectivity. It is
--     therefore a synchronised document, not a plain row: it carries the moment
--     it changed on the device, the moment it changed on the server, and a link
--     to the version it replaced. Conflict rule (question P8, closed on
--     10 August 2026): the inspector's record wins and BOTH versions are kept.
--   * Media is evidence. Without capture time, GPS, device and hash a photo
--     proves nothing, so all four are mandatory.
--   * The checklist is a constructor. Its items are data in jsonb, not columns,
--     because inspection forms differ per activity type and are still changing.

-- ---------------------------------------------------------------------------
-- Checklist templates. Specification module 10.8, "checklist constructor".
-- ---------------------------------------------------------------------------

CREATE TABLE insp.checklist_template (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code            text NOT NULL,                  -- stable across versions
    version         int  NOT NULL DEFAULT 1,
    name            text NOT NULL,
    purpose         text NOT NULL,                  -- SITE_VISIT | FIELD_MONITORING
    activity_type   text,                           -- NULL means "any activity"
    -- The composition of the checklist is data, never columns. An ordered array
    -- of items, each with its own code, label, answer type and scoring rules.
    -- Adding a checklist for a new activity type must not require a migration.
    items           jsonb NOT NULL DEFAULT '[]'::jsonb,
    status          text NOT NULL DEFAULT 'DRAFT',  -- DRAFT | PUBLISHED | ARCHIVED
    organization_id uuid REFERENCES iam.organization(id),
    effective_from  date,
    effective_to    date,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      uuid,

    CONSTRAINT checklist_template_code_version_unique UNIQUE (code, version),
    CONSTRAINT checklist_template_version_positive CHECK (version > 0),
    CONSTRAINT checklist_template_purpose_known CHECK (
        purpose IN ('SITE_VISIT', 'FIELD_MONITORING')
    ),
    CONSTRAINT checklist_template_activity_known CHECK (
        activity_type IS NULL OR activity_type IN (
            'GRAZING', 'HAYMAKING', 'BEEKEEPING',
            'RECREATION', 'FIREWOOD', 'RESEARCH'
        )
    ),
    CONSTRAINT checklist_template_status_known CHECK (
        status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')
    ),
    CONSTRAINT checklist_template_items_is_array CHECK (
        jsonb_typeof(items) = 'array'
    ),
    CONSTRAINT checklist_template_period_ordered CHECK (
        effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from
    )
);

CREATE INDEX checklist_template_lookup
    ON insp.checklist_template (purpose, activity_type)
    WHERE status = 'PUBLISHED';

-- ---------------------------------------------------------------------------
-- Inspection tasks. Scenario S6: a site visit is assigned with a two working
-- day deadline; the inspector receives it in the PWA and drives out.
-- ---------------------------------------------------------------------------

CREATE TABLE insp.inspection_task (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number                text NOT NULL,
    task_type             text NOT NULL,   -- SITE_VISIT | FIELD_MONITORING | COMPLAINT | SCHEDULED
    -- What is being inspected. At least one target is required: a task with no
    -- target cannot be executed and cannot be reported on.
    application_id        uuid REFERENCES app.application(id),
    permit_id             uuid REFERENCES permit.permit(id),
    contour_id            uuid REFERENCES geo.contour(id),
    organization_id       uuid NOT NULL REFERENCES iam.organization(id),
    territory_code        text NOT NULL,
    assignee_id           uuid REFERENCES iam.user_account(id),
    checklist_template_id uuid REFERENCES insp.checklist_template(id),
    status                text NOT NULL DEFAULT 'DRAFT',
    priority              text NOT NULL DEFAULT 'NORMAL',
    scheduled_for         date,
    -- Specification clause 4.2.14: a site visit takes no more than two working
    -- days from the moment the task is issued.
    due_at                timestamptz NOT NULL,
    started_at            timestamptz,
    completed_at          timestamptz,
    note                  text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            uuid,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            uuid,

    CONSTRAINT inspection_task_number_unique UNIQUE (number),
    CONSTRAINT inspection_task_type_known CHECK (
        task_type IN ('SITE_VISIT', 'FIELD_MONITORING', 'COMPLAINT', 'SCHEDULED')
    ),
    CONSTRAINT inspection_task_status_known CHECK (
        status IN ('DRAFT', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')
    ),
    CONSTRAINT inspection_task_priority_known CHECK (
        priority IN ('LOW', 'NORMAL', 'HIGH')
    ),
    CONSTRAINT inspection_task_has_target CHECK (
        application_id IS NOT NULL OR permit_id IS NOT NULL OR contour_id IS NOT NULL
    ),
    CONSTRAINT inspection_task_assigned_has_assignee CHECK (
        status = 'DRAFT' OR assignee_id IS NOT NULL
    ),
    CONSTRAINT inspection_task_completed_has_moment CHECK (
        (status = 'COMPLETED') = (completed_at IS NOT NULL)
    )
);

-- The inspector's own worklist in the PWA.
CREATE INDEX inspection_task_by_assignee
    ON insp.inspection_task (assignee_id, due_at)
    WHERE status IN ('ASSIGNED', 'IN_PROGRESS');

-- Supervisor's overdue list, and the territory filter of the prosecutor.
CREATE INDEX inspection_task_worklist
    ON insp.inspection_task (territory_code, status, due_at);

CREATE INDEX inspection_task_by_permit
    ON insp.inspection_task (permit_id)
    WHERE permit_id IS NOT NULL;

CREATE INDEX inspection_task_by_application
    ON insp.inspection_task (application_id)
    WHERE application_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Electronic inspection acts. Scenario S15.
-- ---------------------------------------------------------------------------

CREATE TABLE insp.inspection_act (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number                text NOT NULL,
    task_id               uuid REFERENCES insp.inspection_task(id),
    permit_id             uuid REFERENCES permit.permit(id),
    application_id        uuid REFERENCES app.application(id),
    contour_id            uuid REFERENCES geo.contour(id),
    organization_id       uuid NOT NULL REFERENCES iam.organization(id),
    -- Denormalised for the prosecutor's row-level security policy in 97-rls.sql.
    -- Without it the policy becomes a join subquery and the "search under three
    -- seconds" requirement of clause 4.1.4 is lost.
    territory_code        text NOT NULL,
    inspector_id          uuid NOT NULL REFERENCES iam.user_account(id),
    inspected_at          timestamptz NOT NULL,

    -- Where the inspector stood. A point, not a polygon: it is a GPS fix, and
    -- the geodesic distance from it to the contour is computed with
    -- ST_Distance over geography. Specification clause 4.3.1.
    gps_point             geometry(Point, 4326),
    gps_accuracy_m        numeric(6,2),
    distance_to_contour_m numeric(12,2),
    -- Clause 15.2: standing outside the contour is recorded in the act itself,
    -- not only shown on screen.
    outside_contour       boolean NOT NULL DEFAULT false,

    -- The act stores which checklist version it was filled against, so an act
    -- drawn up a year ago can still be read exactly as it was answered.
    checklist_template_id uuid REFERENCES insp.checklist_template(id),
    checklist_version     int,
    checklist_result      jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Observed facts: head count by livestock kind, occupied area, number of
    -- hives, volume of hay. Composition follows the activity type, so it is
    -- data rather than columns.
    observed_facts        jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_sb_load      numeric(12,2),   -- conditional heads found on site
    allowed_sb_load       numeric(12,2),   -- conditional heads the norm permits
    observed_area_ha      numeric(12,4),

    -- Specification state machine: compliant | warning | violation. VIOLATION
    -- opens a case and feeds the digital oversight stream.
    verdict               text NOT NULL,
    -- Clause 15.4: no permit found, the act is drawn up as "activity without a
    -- permit". In that case permit_id stays NULL by design.
    without_permit        boolean NOT NULL DEFAULT false,
    findings              text,
    status                text NOT NULL DEFAULT 'DRAFT',

    -- The signature record itself lives in the permit schema and is referenced
    -- by identifier only: the inspection module must not reach into another
    -- module's tables through a foreign key.
    signature_id          uuid,
    signed_at             timestamptz,

    -- Offline synchronisation. The act is filled in the field with no
    -- connectivity and uploaded later.
    device_id             text,
    -- Identifier minted on the device. Makes the upload idempotent: a retried
    -- synchronisation cannot create a second copy of the same act.
    device_record_id      uuid,
    device_updated_at     timestamptz,   -- moment of change on the device
    server_updated_at     timestamptz NOT NULL DEFAULT now(),
    synced_at             timestamptz,
    sync_status           text NOT NULL DEFAULT 'SYNCED',
    -- Conflict rule, question P8, closed on 10 August 2026: the inspector's
    -- record wins and both versions are kept. The losing version stays as a row
    -- with is_current = false and is pointed at by the winner's supersedes_id.
    conflict_resolution   text NOT NULL DEFAULT 'NO_CONFLICT',
    version_origin        text NOT NULL DEFAULT 'SERVER',
    supersedes_id         uuid REFERENCES insp.inspection_act(id),
    is_current            boolean NOT NULL DEFAULT true,

    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            uuid,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            uuid,

    CONSTRAINT inspection_act_number_unique UNIQUE (number),
    -- One act per record minted on a device: replays of the same upload are
    -- rejected by the database rather than by the endpoint.
    CONSTRAINT inspection_act_device_record_unique UNIQUE (device_id, device_record_id),
    -- A version can replace at most one predecessor, so the history is a chain
    -- and not a tangle.
    CONSTRAINT inspection_act_supersedes_unique UNIQUE (supersedes_id),
    CONSTRAINT inspection_act_verdict_known CHECK (
        verdict IN ('COMPLIANT', 'WARNING', 'VIOLATION')
    ),
    CONSTRAINT inspection_act_status_known CHECK (
        status IN ('DRAFT', 'SIGNED', 'SUPERSEDED', 'ARCHIVED')
    ),
    CONSTRAINT inspection_act_sync_status_known CHECK (
        sync_status IN ('PENDING', 'SYNCED', 'CONFLICT')
    ),
    CONSTRAINT inspection_act_conflict_resolution_known CHECK (
        conflict_resolution IN ('NO_CONFLICT', 'DEVICE_WINS')
    ),
    CONSTRAINT inspection_act_version_origin_known CHECK (
        version_origin IN ('DEVICE', 'SERVER')
    ),
    CONSTRAINT inspection_act_geometry_valid CHECK (
        gps_point IS NULL OR ST_IsValid(gps_point)
    ),
    -- An act may be started without a fix, but it cannot be signed without one:
    -- the position is the evidence that the inspector was actually there.
    CONSTRAINT inspection_act_signed_has_position CHECK (
        status = 'DRAFT' OR gps_point IS NOT NULL
    ),
    CONSTRAINT inspection_act_signed_has_signature CHECK (
        status = 'DRAFT' OR (signature_id IS NOT NULL AND signed_at IS NOT NULL)
    ),
    CONSTRAINT inspection_act_checklist_version_paired CHECK (
        (checklist_template_id IS NULL) = (checklist_version IS NULL)
    ),
    -- Either the act is about a permit, or about an application under review
    -- (scenario S6, site visit), or it is explicitly an "activity without a
    -- permit" case. An act about nothing at all is a data entry error.
    CONSTRAINT inspection_act_has_subject CHECK (
        permit_id IS NOT NULL OR application_id IS NOT NULL OR without_permit
    ),
    CONSTRAINT inspection_act_superseded_not_current CHECK (
        status <> 'SUPERSEDED' OR NOT is_current
    ),
    CONSTRAINT inspection_act_sb_load_non_negative CHECK (
        (observed_sb_load IS NULL OR observed_sb_load >= 0)
        AND (allowed_sb_load IS NULL OR allowed_sb_load >= 0)
    )
);

-- Geodesic distance from the inspector to the contour, task 5.5.
CREATE INDEX inspection_act_gps_gix
    ON insp.inspection_act USING gist (gps_point);

CREATE INDEX inspection_act_by_permit
    ON insp.inspection_act (permit_id, inspected_at DESC)
    WHERE permit_id IS NOT NULL;

CREATE INDEX inspection_act_by_inspector
    ON insp.inspection_act (inspector_id, inspected_at DESC);

-- Prosecutor's showcase: territory plus period, clause 4.2.11.2.
CREATE INDEX inspection_act_oversight
    ON insp.inspection_act (territory_code, inspected_at DESC);

-- Feed for the violation workflow.
CREATE INDEX inspection_act_violations
    ON insp.inspection_act (inspected_at DESC)
    WHERE verdict = 'VIOLATION' AND is_current;

-- Acts waiting to be reconciled after coming back from a device.
CREATE INDEX inspection_act_unsynced
    ON insp.inspection_act (server_updated_at)
    WHERE sync_status <> 'SYNCED';

-- ---------------------------------------------------------------------------
-- Media. Specification module 10.8: capture time, GPS, device and hash are all
-- mandatory. Without them the act loses its evidential value.
--
-- The link to the owning object is polymorphic on purpose: the same upload
-- pipeline serves tasks, acts and violation cases, and a foreign key per target
-- would mean three nullable columns and three indexes.
-- ---------------------------------------------------------------------------

CREATE TABLE insp.media (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_type    text NOT NULL,   -- INSPECTION_TASK | INSPECTION_ACT | VIOLATION_CASE
    object_id      uuid NOT NULL,
    media_type     text NOT NULL,   -- PHOTO | VIDEO | AUDIO | DOCUMENT
    storage_bucket text NOT NULL,
    storage_key    text NOT NULL,
    file_name      text NOT NULL,
    mime_type      text NOT NULL,
    size_bytes     bigint NOT NULL,

    -- Integrity of the evidence. Mandatory: a file whose hash is unknown cannot
    -- be shown to have survived unchanged, so it is not evidence.
    hash           text NOT NULL,
    hash_algorithm text NOT NULL DEFAULT 'SHA-256',

    -- Provenance of the evidence, all four required by module 10.8.
    captured_at    timestamptz NOT NULL,
    gps_point      geometry(Point, 4326) NOT NULL,
    gps_accuracy_m numeric(6,2),
    device_id      text NOT NULL,
    device_model   text,

    caption        text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid,

    -- The same file attached twice to the same object is the same evidence.
    CONSTRAINT media_object_hash_unique UNIQUE (object_type, object_id, hash),
    CONSTRAINT media_object_type_known CHECK (
        object_type IN ('INSPECTION_TASK', 'INSPECTION_ACT', 'VIOLATION_CASE')
    ),
    CONSTRAINT media_type_known CHECK (
        media_type IN ('PHOTO', 'VIDEO', 'AUDIO', 'DOCUMENT')
    ),
    CONSTRAINT media_hash_present CHECK (length(hash) > 0),
    CONSTRAINT media_hash_algorithm_known CHECK (
        hash_algorithm IN ('SHA-256', 'SHA-512')
    ),
    CONSTRAINT media_size_positive CHECK (size_bytes > 0),
    CONSTRAINT media_geometry_valid CHECK (ST_IsValid(gps_point))
);

CREATE INDEX media_by_object
    ON insp.media (object_type, object_id, captured_at);

CREATE INDEX media_gps_gix
    ON insp.media USING gist (gps_point);

-- Integrity audits walk the whole set by hash.
CREATE INDEX media_by_hash
    ON insp.media (hash);

-- ---------------------------------------------------------------------------
-- Violation cases. Scenario S16: a case is opened the moment an act is signed
-- with the "violation" verdict; explanation is due in five working days and the
-- decision in ten.
-- ---------------------------------------------------------------------------

CREATE TABLE insp.violation_case (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Single case number, assigned on opening. Scenario S16 step 1.
    number                  text NOT NULL,
    act_id                  uuid NOT NULL REFERENCES insp.inspection_act(id),
    permit_id               uuid REFERENCES permit.permit(id),
    application_id          uuid REFERENCES app.application(id),
    contour_id              uuid REFERENCES geo.contour(id),
    organization_id         uuid NOT NULL REFERENCES iam.organization(id),
    territory_code          text NOT NULL,

    -- Code from the "violation types" classifier, which the Agency owns.
    -- Held as a code rather than a foreign key so that a retired classifier
    -- value cannot rewrite the history of a closed case.
    violation_type          text NOT NULL,
    legal_base              text,
    severity                text NOT NULL DEFAULT 'MEDIUM',
    summary                 text,

    opened_at               timestamptz NOT NULL DEFAULT now(),
    -- Five working days for the explanation, ten for the decision. Computed by
    -- the application against the working calendar, stored as absolute moments.
    explanation_due_at      timestamptz,
    explanation_received_at timestamptz,
    explanation_text        text,
    decision_due_at         timestamptz,

    -- Damage caused by the violation. Money is numeric(18,2) and nothing else:
    -- the legacy system rounded through int() and lost tiyin.
    damage_amount           numeric(18,2),
    -- Every input the damage figure was derived from, so the sum can be
    -- explained years later without rerunning the code that produced it.
    damage_calculation      jsonb,

    decision                text,
    decision_note           text,
    decided_at              timestamptz,
    decided_by              uuid REFERENCES iam.user_account(id),

    status                  text NOT NULL DEFAULT 'OPENED',
    -- Clause 16.2: a repeat offence shows the history and suggests a stricter
    -- measure. The flag is set when the case is opened; the history itself is
    -- read from the earlier cases of the same permit or applicant.
    is_repeated             boolean NOT NULL DEFAULT false,
    repeat_count            int NOT NULL DEFAULT 0,
    -- Clause 16.3: an appeal is kept as its own record in the case.
    appealed_at             timestamptz,
    appeal_note             text,
    closed_at               timestamptz,
    archived_at             timestamptz,

    created_at              timestamptz NOT NULL DEFAULT now(),
    created_by              uuid,
    updated_at              timestamptz NOT NULL DEFAULT now(),
    updated_by              uuid,

    CONSTRAINT violation_case_number_unique UNIQUE (number),
    CONSTRAINT violation_case_severity_known CHECK (
        severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')
    ),
    CONSTRAINT violation_case_decision_known CHECK (
        decision IS NULL OR decision IN (
            'WARNING',
            'PERMIT_SUSPENDED',
            'PERMIT_REVOKED',
            'DAMAGE_CLAIMED',
            'REFERRED_TO_AUTHORITIES',
            'NO_ACTION'
        )
    ),
    CONSTRAINT violation_case_status_known CHECK (
        status IN (
            'OPENED',
            'EXPLANATION_REQUESTED',
            'UNDER_REVIEW',
            'DECIDED',
            'REMEDIED',
            'APPEALED',
            'CLOSED',
            'ARCHIVED'
        )
    ),
    CONSTRAINT violation_case_decision_paired CHECK (
        (decision IS NULL) = (decided_at IS NULL)
    ),
    CONSTRAINT violation_case_damage_non_negative CHECK (
        damage_amount IS NULL OR damage_amount >= 0
    ),
    CONSTRAINT violation_case_repeat_count_non_negative CHECK (repeat_count >= 0),
    CONSTRAINT violation_case_repeat_flag_agrees CHECK (
        is_repeated = (repeat_count > 0)
    ),
    CONSTRAINT violation_case_closed_has_moment CHECK (
        status NOT IN ('CLOSED', 'ARCHIVED') OR closed_at IS NOT NULL
    ),
    CONSTRAINT violation_case_archived_has_moment CHECK (
        (status = 'ARCHIVED') = (archived_at IS NOT NULL)
    )
);

CREATE INDEX violation_case_by_act
    ON insp.violation_case (act_id);

-- Repeat offence lookup, clause 16.2.
CREATE INDEX violation_case_by_permit
    ON insp.violation_case (permit_id, opened_at DESC)
    WHERE permit_id IS NOT NULL;

-- Prosecutor's territory filter and the supervisor's open-case list.
CREATE INDEX violation_case_oversight
    ON insp.violation_case (territory_code, status, opened_at DESC);

-- Cases whose decision deadline is running.
CREATE INDEX violation_case_due
    ON insp.violation_case (decision_due_at)
    WHERE status IN ('OPENED', 'EXPLANATION_REQUESTED', 'UNDER_REVIEW');
