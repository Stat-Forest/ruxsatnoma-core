-- One schema per module. The mapping is enforced, not conventional:
-- if a module and a schema drift apart, the boundary cannot be checked.
-- See architecture/modules.md in the documentation repository.

CREATE SCHEMA IF NOT EXISTS nsi;     -- classifiers and reference data
CREATE SCHEMA IF NOT EXISTS iam;     -- users, roles, organizations, applicants
CREATE SCHEMA IF NOT EXISTS geo;     -- layers, contours, occupancy
CREATE SCHEMA IF NOT EXISTS rules;   -- norms, tariffs, calendars, base unit history
CREATE SCHEMA IF NOT EXISTS app;     -- applications, workflow, calculations
CREATE SCHEMA IF NOT EXISTS permit;  -- permits, templates, signatures, forest tickets
CREATE SCHEMA IF NOT EXISTS pay;     -- invoices, transactions, reconciliation, refunds
CREATE SCHEMA IF NOT EXISTS insp;    -- inspection tasks, acts, violations
CREATE SCHEMA IF NOT EXISTS rep;     -- report forms and filled reports
CREATE SCHEMA IF NOT EXISTS arch;    -- archive
CREATE SCHEMA IF NOT EXISTS audit;   -- append-only audit log, risk indicators, outbox
