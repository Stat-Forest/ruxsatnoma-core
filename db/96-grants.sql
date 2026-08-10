-- Privileges. Applied after every table exists.
--
-- Two connection pools live in core: the regular one under app_core and a
-- separate one under oversight_ro. Requests made under the Prosecutor role are
-- routed to the second. This is how specification appendix 6 is satisfied
-- without standing up a separate service.

-- Owner of the domain schemas: full DML.
GRANT USAGE ON SCHEMA nsi, iam, geo, rules, app, permit, pay, insp, rep, arch, audit TO app_core;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA
    nsi, iam, geo, rules, app, permit, pay, insp, rep, arch, audit TO app_core;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA
    nsi, iam, geo, rules, app, permit, pay, insp, rep, arch, audit TO app_core;

-- Prosecutor: read-only, no write privileges whatsoever.
GRANT USAGE ON SCHEMA nsi, iam, geo, rules, app, permit, pay, insp, rep, arch, audit TO oversight_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA
    nsi, iam, geo, rules, app, permit, pay, insp, rep, arch, audit TO oversight_ro;

-- Tables created later by app_core stay readable without another grant.
ALTER DEFAULT PRIVILEGES FOR ROLE app_core IN SCHEMA
    nsi, iam, geo, rules, app, permit, pay, insp, rep, arch, audit
    GRANT SELECT ON TABLES TO oversight_ro;

-- Belt and braces: an accidental GRANT elsewhere still leaves no write access.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA
    nsi, iam, geo, rules, app, permit, pay, insp, rep, arch, audit FROM oversight_ro;

-- The audit log is append-only for everyone, including the schema owner and
-- the system administrator. Specification clause 4.2.4. The trigger defined in
-- 60-audit.sql enforces the same rule a second time, on purpose: privileges can
-- be granted back by mistake, the trigger cannot be bypassed by a GRANT.
REVOKE UPDATE, DELETE, TRUNCATE ON audit.audit_log FROM PUBLIC, app_core;

-- Migration scripts: write access only to what the cutover actually touches,
-- and only during the cutover window.
GRANT USAGE ON SCHEMA nsi, iam, geo, rules, app, permit, pay TO migrator;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA
    nsi, iam, geo, rules, app, permit, pay TO migrator;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA
    nsi, iam, geo, rules, app, permit, pay TO migrator;
