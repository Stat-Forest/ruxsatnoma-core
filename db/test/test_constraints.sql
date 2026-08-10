-- Invariants that must hold at the database level, not in application code.
-- Run against a freshly initialised ruxsatnoma_core.
--
-- Each check either prints OK or aborts the script. Silence means failure.
\set ON_ERROR_STOP on

BEGIN;

-- Fixtures. Rolled back at the end, nothing is left behind.
INSERT INTO iam.organization (id, code, name, type, territory_code)
VALUES ('aaaaaaaa-0000-0000-0000-000000000001', 'TEST-ORG', 'Test forestry', 'FORESTRY', '01');

-- An individual applicant must carry a PINFL: applicant_identifier_present.
INSERT INTO iam.applicant (id, type, full_name, pinfl, territory_code)
VALUES ('bbbbbbbb-0000-0000-0000-000000000001', 'INDIVIDUAL', 'Test applicant',
        '12345678901234', '01');

INSERT INTO geo.contour (id, layer_id, organization_id, territory_code)
SELECT 'cccccccc-0000-0000-0000-000000000001', l.id,
       'aaaaaaaa-0000-0000-0000-000000000001', '01'
FROM geo.layer l WHERE l.code = 'CONTOUR' LIMIT 1;

-- 1. Specification module 10.1: one active application per applicant, contour,
--    activity type and overlapping period. Required to be a database constraint.
INSERT INTO app.application
    (applicant_id, organization_id, territory_code, activity_type, contour_id, period, status, channel)
VALUES ('bbbbbbbb-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
        '01', 'GRAZING', 'cccccccc-0000-0000-0000-000000000001',
        daterange('2026-05-01','2026-09-01'), 'SUBMITTED', 'PORTAL');

DO $$
BEGIN
    INSERT INTO app.application
        (applicant_id, organization_id, territory_code, activity_type, contour_id, period, status, channel)
    VALUES ('bbbbbbbb-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
            '01', 'GRAZING', 'cccccccc-0000-0000-0000-000000000001',
            daterange('2026-07-01','2026-10-01'), 'SUBMITTED', 'PORTAL');
    RAISE EXCEPTION 'FAIL: an overlapping duplicate application was accepted';
EXCEPTION WHEN exclusion_violation THEN
    RAISE NOTICE 'OK 1: overlapping duplicate application rejected';
END $$;

-- 2. A terminal status must not block a new application for the same key.
-- A rejected application must carry a reason code: application_reject_reason_required.
INSERT INTO app.application
    (applicant_id, organization_id, territory_code, activity_type, contour_id, period,
     status, channel, reject_reason)
VALUES ('bbbbbbbb-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
        '01', 'HAYMAKING', 'cccccccc-0000-0000-0000-000000000001',
        daterange('2026-05-01','2026-09-01'), 'REJECTED', 'PORTAL', 'RJ-06');

INSERT INTO app.application
    (applicant_id, organization_id, territory_code, activity_type, contour_id, period, status, channel)
VALUES ('bbbbbbbb-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
        '01', 'HAYMAKING', 'cccccccc-0000-0000-0000-000000000001',
        daterange('2026-05-01','2026-09-01'), 'SUBMITTED', 'PORTAL');

DO $$ BEGIN RAISE NOTICE 'OK 2: a rejected application does not block a new one'; END $$;

ROLLBACK;

-- 3. Specification clause 4.2.4: the audit log cannot be updated.
INSERT INTO audit.audit_log (occurred_at, action, object_type, territory_code)
VALUES (now(), 'TEST', 'test', '01');

DO $$
BEGIN
    UPDATE audit.audit_log SET action = 'tampered' WHERE action = 'TEST';
    RAISE EXCEPTION 'FAIL: the audit log was updated';
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK 3: audit log rejects UPDATE';
END $$;

-- 4. The audit log cannot be deleted from either.
DO $$
BEGIN
    DELETE FROM audit.audit_log WHERE action = 'TEST';
    RAISE EXCEPTION 'FAIL: the audit log was deleted from';
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK 4: audit log rejects DELETE';
END $$;

-- 5. Nor truncated: a row-level trigger never fires on TRUNCATE.
DO $$
BEGIN
    TRUNCATE audit.audit_log;
    RAISE EXCEPTION 'FAIL: the audit log was truncated';
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK 5: audit log rejects TRUNCATE';
END $$;

-- 6. Scenario S11: permit series and number are unique, guaranteed by the
--    database. Asserted on the catalogue rather than by inserting a permit,
--    because a permit needs a whole approved application behind it.
DO $$
DECLARE
    has_unique boolean;
    has_gis_bound boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'permit.permit'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid) ILIKE '%series%number%'
    ) INTO has_unique;

    IF NOT has_unique THEN
        RAISE EXCEPTION 'FAIL: permit series and number are not unique at the database level';
    END IF;

    -- Permit must be bound to valid geometry while active. KPI: 100 percent.
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'permit.permit'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%st_isvalid%'
    ) INTO has_gis_bound;

    IF NOT has_gis_bound THEN
        RAISE EXCEPTION 'FAIL: an active permit is not bound to valid geometry';
    END IF;

    RAISE NOTICE 'OK 6: permit number is unique and bound to valid geometry';
END $$;

-- 7. Money is never floating point. A single float column would silently lose
--    tiat, which is exactly how the legacy system lost money.
DO $$
DECLARE
    bad_columns int;
BEGIN
    SELECT count(*) INTO bad_columns
    FROM information_schema.columns
    WHERE table_schema IN ('app','pay','permit','rules','insp')
      AND data_type IN ('real','double precision','money')
      AND (column_name LIKE '%amount%' OR column_name LIKE '%sum%'
           OR column_name LIKE '%price%' OR column_name LIKE '%fee%');
    IF bad_columns > 0 THEN
        RAISE EXCEPTION 'FAIL: % monetary columns are not numeric', bad_columns;
    END IF;
    RAISE NOTICE 'OK 7: every monetary column is numeric';
END $$;

-- 8. Appendix 6: the prosecutor role holds no write privileges anywhere.
DO $$
DECLARE
    writable int;
BEGIN
    SELECT count(*) INTO writable
    FROM information_schema.table_privileges
    WHERE grantee = 'oversight_ro'
      AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE');
    IF writable > 0 THEN
        RAISE EXCEPTION 'FAIL: oversight_ro holds % write privileges', writable;
    END IF;
    RAISE NOTICE 'OK 8: oversight_ro has no write privileges';
END $$;

-- Clean up the audit row the only way allowed: it stays. The audit log is
-- append-only by design, so the test row remains and that is correct behaviour.
