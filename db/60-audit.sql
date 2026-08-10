-- Schema `audit`: the append-only journal, risk indicators, the outbox and
-- notifications. Specification clauses 4.2.4 (immutable audit), 4.2.8
-- (notifications) and 4.2.11 (digital oversight, appendix 6.2).
--
-- Two properties shape everything below.
--
-- 1. Nothing here may be rewritten. The journal is evidence: clause 4.2.4 states
--    that not even a system administrator may alter or delete it. Immutability is
--    enforced by the database, not by the application.
-- 2. Nothing here points at another schema with a foreign key. The journal
--    outlives the objects it describes, including deleted ones, so object
--    references are polymorphic: object_type + object_id, no REFERENCES.
--
-- audit_log, risk_indicator and oversight_event are journals: they record the
-- moment an event happened and who caused it, and therefore carry no updated_at
-- or updated_by. outbox_message and notification are delivery state machines and
-- do change after insert.


-- ---------------------------------------------------------------------------
-- audit.audit_log
-- ---------------------------------------------------------------------------

-- territory_code is denormalised on purpose. The prosecutor's row-level security
-- policy filters by it directly; resolving the territory through a join would
-- turn every policy check into a subquery and break the "search <= 3 seconds"
-- requirement of clause 4.1.4. A NULL means a republic-level action, visible
-- only to a prosecutor whose scope is REPUBLIC. The policy itself is created
-- with the rest of the RLS rules, once every table exists.
CREATE TABLE audit.audit_log (
    id            bigint GENERATED ALWAYS AS IDENTITY,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    actor_id      uuid,
    actor_role    text,
    territory_code text,
    action        text NOT NULL,
    object_type   text NOT NULL,
    object_id     uuid,
    old_value     jsonb,
    new_value     jsonb,
    ip            inet,
    device        text,
    correlation_id uuid,
    legal_base    text,
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

COMMENT ON TABLE audit.audit_log IS
    'Append-only journal of every legally significant action. Clause 4.2.4: '
    'may not be altered or deleted, not even by an administrator. '
    'Object references are polymorphic and carry no foreign keys.';

COMMENT ON COLUMN audit.audit_log.territory_code IS
    'Denormalised for the prosecutor row-level security policy. NULL means a '
    'republic-level action.';


-- ---------------------------------------------------------------------------
-- Immutability. The most important part of this file.
-- ---------------------------------------------------------------------------

-- Two independent mechanisms, because either one alone has a hole.
--
--   * The REVOKE stops anyone who holds no more than the granted privileges.
--     It does not stop the table owner or a superuser, and it is undone by any
--     later blanket GRANT on the schema.
--   * The trigger stops everyone, owner and superuser included, and no GRANT
--     can weaken it.
--
-- Neither is redundant: the privilege check is cheap and fails early, the
-- trigger is the guarantee.
REVOKE UPDATE, DELETE, TRUNCATE ON audit.audit_log FROM PUBLIC, app_core;

CREATE OR REPLACE FUNCTION audit.deny_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (RI-06)'
        USING ERRCODE = 'insufficient_privilege';
END $$ LANGUAGE plpgsql;

COMMENT ON FUNCTION audit.deny_mutation() IS
    'Rejects any attempt to modify the audit journal. The application catches '
    'insufficient_privilege here and raises risk indicator RI-06, critical '
    'level, with an immediate SOC alert.';

-- The row-level trigger is attached to the PARENT partitioned table, never to
-- individual partitions. Verified on PostgreSQL 18.1 on 10 August 2026: a row
-- trigger declared on a partitioned table fires on every partition and is
-- cloned automatically into partitions created later. Attaching it per
-- partition instead would turn every partition the scheduled job forgets into a
-- hole in the audit protection.
CREATE TRIGGER audit_log_immutable
    BEFORE UPDATE OR DELETE ON audit.audit_log
    FOR EACH ROW EXECUTE FUNCTION audit.deny_mutation();

-- ENABLE ALWAYS makes the trigger fire even under
-- session_replication_role = 'replica', which a superuser could otherwise set
-- to switch ordinary triggers off. Verified to propagate to partitions created
-- afterwards, exactly like the trigger itself.
ALTER TABLE audit.audit_log ENABLE ALWAYS TRIGGER audit_log_immutable;

-- A row-level trigger never fires on TRUNCATE, so that path needs its own
-- statement-level trigger. It is not cloned into partitions, but it does not
-- have to be: a freshly created partition carries no privileges for anyone
-- except its owner, so nobody can truncate it, and the REVOKE below closes the
-- partitions created here against a later blanket GRANT on the schema.
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.deny_mutation();


-- ---------------------------------------------------------------------------
-- Monthly partitions
-- ---------------------------------------------------------------------------

-- Retention is at least 3 years, which is 36 live partitions. The twelve months
-- created here run from August 2026 to July 2027; the rest are created by a
-- scheduled `core-worker` job. Because the trigger above lives on the parent,
-- that job creates partitions and nothing else.
--
-- Boundaries are at midnight Tashkent time (+05, no daylight saving), so a
-- partition holds exactly one calendar month as the specification displays it
-- under clause 4.3.6, not one month shifted by the UTC offset.
--
-- There is deliberately NO default partition. A row outside every range must
-- fail loudly so the missing partition gets created, and a default partition
-- would be a one-way trap: rows landing in it can only be moved out with a
-- DELETE, which the immutability trigger forbids, and until they are moved the
-- covering partition cannot be attached.
DO $$
DECLARE
    first_month constant date := date '2026-08-01';
    month_start date;
    part_name   text;
BEGIN
    FOR i IN 0..11 LOOP
        month_start := first_month + (i * interval '1 month');
        part_name := 'audit_log_' || to_char(month_start, 'YYYY_MM');

        EXECUTE format(
            'CREATE TABLE audit.%I PARTITION OF audit.audit_log '
            'FOR VALUES FROM (%L) TO (%L)',
            part_name,
            to_char(month_start, 'YYYY-MM-DD') || ' 00:00:00+05',
            to_char(month_start + interval '1 month', 'YYYY-MM-DD') || ' 00:00:00+05'
        );

        EXECUTE format(
            'REVOKE UPDATE, DELETE, TRUNCATE ON audit.%I FROM PUBLIC, app_core',
            part_name
        );
    END LOOP;
END $$;


-- ---------------------------------------------------------------------------
-- audit.audit_log indexes
-- ---------------------------------------------------------------------------

-- Declared on the parent, so every partition gets its own copy, now and later.
-- Both serve prosecutor filters, clause 4.1.4: search <= 3 seconds.
CREATE INDEX audit_by_actor  ON audit.audit_log (actor_id, occurred_at DESC);
CREATE INDEX audit_by_object ON audit.audit_log (object_type, object_id, occurred_at DESC);


-- ---------------------------------------------------------------------------
-- audit.risk_indicator
-- ---------------------------------------------------------------------------

-- Catalogue of appendix 6.2, fifteen indicators. Code, level and delivery mode
-- as specified:
--   RI-01 PAID without provider or bank confirmation          HIGH      IMMEDIATE
--   RI-02 permit issued beyond the norm or limit              HIGH      IMMEDIATE
--   RI-03 overlapping active permits on one contour           HIGH      IMMEDIATE
--   RI-04 retroactive change of a tariff or a norm            HIGH      IMMEDIATE
--   RI-05 signing with a revoked or expired certificate       HIGH      IMMEDIATE
--   RI-06 attempt to alter or delete the audit journal        CRITICAL  IMMEDIATE + SOC
--   RI-07 SLA breach                                          MEDIUM    DAILY_DIGEST
--   RI-08 application passed on an unapproved norm            HIGH      IMMEDIATE
--   RI-09 unusual number of approvals by one employee         MEDIUM    DAILY_DIGEST
--   RI-10 permit activated without payment                    CRITICAL  IMMEDIATE
--   RI-11 refund off the formula                              HIGH      IMMEDIATE
--   RI-12 access attempted outside the territorial scope      HIGH      IMMEDIATE
--   RI-13 permit issued during a fire ban period              HIGH      IMMEDIATE
--   RI-14 permit long active with no inspection result        LOW       MONTHLY_DIGEST
--   RI-15 unusual number of permits for one PINFL or TIN      MEDIUM    DAILY_DIGEST
--
-- The level is stored rather than derived from the code: the catalogue is a
-- regulatory document and may be revised, and a stored level keeps indicators
-- raised under the old catalogue readable as they were raised.
CREATE TABLE audit.risk_indicator (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code          text NOT NULL,
    level         text NOT NULL,
    object_type   text NOT NULL,           -- polymorphic, no foreign key
    object_id     uuid,
    territory_code text,
    actor_id      uuid,                    -- whose action raised the indicator
    detected_at   timestamptz NOT NULL DEFAULT now(),
    description   text NOT NULL,
    details       jsonb NOT NULL DEFAULT '{}'::jsonb,
    correlation_id uuid,
    -- Soft reference into the partitioned journal: both halves of its composite
    -- primary key, still without a foreign key.
    audit_log_id  bigint,
    audit_log_occurred_at timestamptz,
    -- Hand-off to "Raqamli nazorat". Immediate indicators must be there within
    -- 5 minutes; repeated delivery is suppressed by the idempotency key.
    delivery_mode text NOT NULL,
    delivery_status text NOT NULL DEFAULT 'PENDING',
    delivery_attempts int NOT NULL DEFAULT 0,
    last_delivery_attempt_at timestamptz,
    delivered_at  timestamptz,
    idempotency_key text NOT NULL UNIQUE,
    CONSTRAINT risk_indicator_code_known CHECK (code IN (
        'RI-01','RI-02','RI-03','RI-04','RI-05','RI-06','RI-07','RI-08',
        'RI-09','RI-10','RI-11','RI-12','RI-13','RI-14','RI-15'
    )),
    CONSTRAINT risk_indicator_level_known CHECK (level IN (
        'LOW','MEDIUM','HIGH','CRITICAL'
    )),
    CONSTRAINT risk_indicator_delivery_mode_known CHECK (delivery_mode IN (
        'IMMEDIATE','DAILY_DIGEST','MONTHLY_DIGEST'
    )),
    CONSTRAINT risk_indicator_delivery_status_known CHECK (delivery_status IN (
        'PENDING','SENT','FAILED'
    )),
    CONSTRAINT risk_indicator_attempts_non_negative CHECK (delivery_attempts >= 0),
    CONSTRAINT risk_indicator_audit_ref_complete CHECK (
        (audit_log_id IS NULL) = (audit_log_occurred_at IS NULL)
    )
);

COMMENT ON TABLE audit.risk_indicator IS
    'Risk indicators RI-01 through RI-15, appendix 6.2. Raised automatically '
    'and pushed to "Raqamli nazorat" without waiting for a prosecutor request. '
    'The observation is written once; only the delivery columns change after '
    'insert, which is why there is no updated_at.';

CREATE INDEX risk_by_code   ON audit.risk_indicator (code, detected_at DESC);
CREATE INDEX risk_by_object ON audit.risk_indicator (object_type, object_id, detected_at DESC);
CREATE INDEX risk_undelivered ON audit.risk_indicator (detected_at)
    WHERE delivery_status <> 'SENT';


-- ---------------------------------------------------------------------------
-- audit.oversight_event
-- ---------------------------------------------------------------------------

-- Clause 4.2.11.4. Two directions travel through one table: every legally
-- significant action of the system, and every view, search and export performed
-- by a prosecutor. Accountability is two-way, so the prosecutor's own reads go
-- back to the oversight system as well.
--
-- The event type list mirrors the AMQP routing keys of architecture/contracts.md
-- plus the three prosecutor actions. Adding a key means a migration here; that
-- is the intended friction for a journal that is legal evidence.
CREATE TABLE audit.oversight_event (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type    text NOT NULL,
    object_type   text NOT NULL,           -- polymorphic, no foreign key
    object_id     uuid,
    territory_code text,                   -- "Raqamli nazorat" routes by it
    actor_id      uuid,
    actor_role    text,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    correlation_id uuid NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    outbox_message_id uuid,                -- soft link to audit.outbox_message
    delivery_status text NOT NULL DEFAULT 'PENDING',
    delivery_attempts int NOT NULL DEFAULT 0,
    last_delivery_attempt_at timestamptz,
    delivered_at  timestamptz,
    CONSTRAINT oversight_event_type_known CHECK (event_type IN (
        'application.submitted','application.approved','application.rejected',
        'application.returned',
        'permit.issued','permit.suspended','permit.revoked',
        'payment.invoiced','payment.confirmed','payment.refunded',
        'norm.published',
        'audit.recorded',
        'risk.detected',
        'oversight.viewed','oversight.searched','oversight.exported'
    )),
    CONSTRAINT oversight_event_delivery_status_known CHECK (delivery_status IN (
        'PENDING','SENT','FAILED'
    )),
    CONSTRAINT oversight_event_attempts_non_negative CHECK (delivery_attempts >= 0)
);

COMMENT ON TABLE audit.oversight_event IS
    'Events handed to "Raqamli nazorat", clause 4.2.11.4: legally significant '
    'actions of the system and every view, search and export by a prosecutor. '
    'Append-only, like the journal it accompanies.';

CREATE INDEX oversight_by_object ON audit.oversight_event (object_type, object_id, occurred_at DESC);
CREATE INDEX oversight_by_actor  ON audit.oversight_event (actor_id, occurred_at DESC);
CREATE INDEX oversight_undelivered ON audit.oversight_event (occurred_at)
    WHERE delivery_status <> 'SENT';


-- ---------------------------------------------------------------------------
-- audit.outbox_message
-- ---------------------------------------------------------------------------

-- The row is written in the SAME transaction as the action it describes.
-- Anything else loses events when the process dies between the commit and the
-- publish.
CREATE TABLE audit.outbox_message (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    topic          text NOT NULL,          -- events | risk.detected | notify
    routing_key    text NOT NULL,          -- application.submitted, permit.issued ...
    payload        jsonb NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    correlation_id uuid NOT NULL,
    status         text NOT NULL DEFAULT 'PENDING',
    attempts       int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    -- Beyond the shape in architecture/database.md: without these two, a message
    -- that ends up in the dead letter queue cannot be diagnosed.
    sent_at        timestamptz,
    last_error     text,
    CONSTRAINT outbox_message_status_known CHECK (status IN (
        'PENDING','SENT','FAILED'
    )),
    CONSTRAINT outbox_message_attempts_non_negative CHECK (attempts >= 0)
);

COMMENT ON TABLE audit.outbox_message IS
    'Transactional outbox. Written in the same transaction as the action it '
    'describes, published to the `ruxsatnoma` topic exchange by a worker. '
    'Retry is exponential: 1, 2, 4, 8 minutes, then the dead letter queue.';

-- The dispatcher's only query: due messages, oldest first.
CREATE INDEX outbox_pending ON audit.outbox_message (status, next_attempt_at)
    WHERE status = 'PENDING';


-- ---------------------------------------------------------------------------
-- audit.notification
-- ---------------------------------------------------------------------------

-- Why a notification lives in the audit schema.
--
-- In the specification a notification is a cross-cutting entity (recipient,
-- channel, template, status, time), so it belongs to no single domain schema
-- and gets none of its own. It sits next to outbox_message because it travels
-- the same way: a row is written in the transaction that caused the event, and
-- a worker delivers it, retries it and records the outcome. Same delivery
-- machinery, same operational dashboard, same schema.
--
-- The audit schema is otherwise append-only; this table and outbox_message are
-- the two exceptions, and both are delivery state machines rather than journals.
CREATE TABLE audit.notification (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Deliberately no foreign key to iam.user_account: a notification is part of
    -- the delivery record and must survive any change to the recipient's account.
    recipient_id  uuid,
    recipient_address text,                -- phone or email as of the send time
    channel       text NOT NULL,
    template_code text NOT NULL,
    template_version text,
    language      text NOT NULL,
    vars          jsonb NOT NULL DEFAULT '{}'::jsonb,
    subject       text,
    body          text,
    -- A legally significant notification is delivered in-app even when the
    -- recipient has switched the channel off. Clause 4.2.8.
    is_mandatory  boolean NOT NULL DEFAULT false,
    status        text NOT NULL DEFAULT 'QUEUED',
    attempts      int NOT NULL DEFAULT 0,
    sent_at       timestamptz,
    delivered_at  timestamptz,
    failed_at     timestamptz,
    last_error    text,
    -- Set when this row is a retry of a failed notification over a fallback
    -- channel, scenario S19 case 19.1. Points at audit.notification(id).
    fallback_for_id uuid,
    object_type   text,                    -- polymorphic, no foreign key
    object_id     uuid,
    correlation_id uuid NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    -- No created_by or updated_by: notifications are raised by the system, never
    -- by a person. The actor behind the triggering event is in the audit journal.
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT notification_channel_known CHECK (channel IN (
        'IN_APP','SMS','EMAIL','MYGOV_CALLBACK'
    )),
    CONSTRAINT notification_language_known CHECK (language IN (
        'uz-Latn','uz-Cyrl','ru','en'
    )),
    CONSTRAINT notification_status_known CHECK (status IN (
        'QUEUED','SENT','DELIVERED','FAILED'
    )),
    CONSTRAINT notification_attempts_non_negative CHECK (attempts >= 0)
);

COMMENT ON TABLE audit.notification IS
    'Notification delivery record: recipient, channel, template, status, time. '
    'Kept in the audit schema because the specification defines it as a '
    'cross-cutting entity with no home domain, and because it is delivered by '
    'the same worker and retry machinery as audit.outbox_message.';

-- Notification history in the user cabinet, scenario S19 step 6.
CREATE INDEX notification_by_recipient ON audit.notification (recipient_id, created_at DESC)
    WHERE recipient_id IS NOT NULL;

-- The delivery worker's queue: everything not yet delivered, oldest first.
CREATE INDEX notification_pending ON audit.notification (status, created_at)
    WHERE status IN ('QUEUED','FAILED');
