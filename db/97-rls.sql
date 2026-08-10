-- Row-level security for the Prosecutor role.
--
-- The application already filters by territory. These policies are the second
-- line: even a bug in the application cannot show a prosecutor another region's
-- data, because the database itself refuses to return those rows.
--
-- core sets `SET LOCAL app.territory_code` at the start of the transaction from
-- the data returned by "Raqamli nazorat" when the officer was verified.
--
-- Two things that are easy to forget:
--   * A table owner bypasses RLS. app_core owns these schemas, so the policies
--     do not apply to it and normal operation is not slowed down. They do apply
--     to oversight_ro, which owns nothing.
--   * territory_code is denormalised onto every table a prosecutor filters by.
--     Without it the policy turns into a join subquery and breaks the "search
--     under 3 seconds" requirement.

-- Applications.
ALTER TABLE app.application ENABLE ROW LEVEL SECURITY;

CREATE POLICY application_territory_ro ON app.application
    FOR SELECT TO oversight_ro
    USING (
        territory_code = current_setting('app.territory_code', true)
        OR current_setting('app.territory_scope', true) = 'REPUBLIC'
    );

-- Permits.
ALTER TABLE permit.permit ENABLE ROW LEVEL SECURITY;

CREATE POLICY permit_territory_ro ON permit.permit
    FOR SELECT TO oversight_ro
    USING (
        territory_code = current_setting('app.territory_code', true)
        OR current_setting('app.territory_scope', true) = 'REPUBLIC'
    );

-- Invoices.
ALTER TABLE pay.invoice ENABLE ROW LEVEL SECURITY;

CREATE POLICY invoice_territory_ro ON pay.invoice
    FOR SELECT TO oversight_ro
    USING (
        territory_code = current_setting('app.territory_code', true)
        OR current_setting('app.territory_scope', true) = 'REPUBLIC'
    );

-- Inspection acts.
ALTER TABLE insp.inspection_act ENABLE ROW LEVEL SECURITY;

CREATE POLICY inspection_act_territory_ro ON insp.inspection_act
    FOR SELECT TO oversight_ro
    USING (
        territory_code = current_setting('app.territory_code', true)
        OR current_setting('app.territory_scope', true) = 'REPUBLIC'
    );

-- Audit log. Partitioned: the policy on the parent covers every partition,
-- including those created later.
ALTER TABLE audit.audit_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY audit_log_territory_ro ON audit.audit_log
    FOR SELECT TO oversight_ro
    USING (
        territory_code = current_setting('app.territory_code', true)
        OR current_setting('app.territory_scope', true) = 'REPUBLIC'
    );
