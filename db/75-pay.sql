-- Schema `pay`: invoices, payment intents, provider transactions, bank
-- statements, reconciliation, the allocation ledger and refunds. Covers
-- module 10.5 (payment) and module 10.10 (benefits and refund).
--
-- Applied after 70-permit.sql: invoices point at permits.
--
-- The invariant of the whole schema, scenario C9 clause 5: status PAID is set
-- only on a confirmation from a provider or a bank. Manual marking is an
-- exception that requires a bank document, a second person and raises risk
-- indicator RI-01. Both halves of that rule are constrained below.
--
-- House rules that hold for every table here:
--   * primary keys are uuid with gen_random_uuid();
--   * every point in time is timestamptz;
--   * money is numeric(18,2), never float and never int. The legacy system
--     rounded through int() and lost kopecks;
--   * enumerations are text plus a CHECK list, not enum types;
--   * every table carries created_at, updated_at, created_by, updated_by;
--   * invoice and provider_transaction take part in the legacy migration and
--     carry legacy_id and legacy_table.


-- ---------------------------------------------------------------------------
-- pay.invoice
--
-- Built from the calculation snapshot, 100 % prepayment per resolution VMQ 278
-- (scenario C9, clause 1). The amount is copied from the calculation and never
-- recomputed: a later change of a norm or a tariff must not move an issued bill.
-- ---------------------------------------------------------------------------

CREATE TABLE pay.invoice (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number               text NOT NULL,

    application_id       uuid NOT NULL REFERENCES app.application(id),
    calculation_id       uuid REFERENCES app.calculation(id),
    contract_id          uuid REFERENCES app.contract(id),   -- shape pending questions P1 and P5
    permit_id            uuid REFERENCES permit.permit(id),  -- filled once the permit is issued
    applicant_id         uuid NOT NULL REFERENCES iam.applicant(id),
    organization_id      uuid NOT NULL REFERENCES iam.organization(id),
    territory_code       text NOT NULL,                      -- denormalised for ABAC, RLS and the prosecutor filter

    invoice_type         text NOT NULL DEFAULT 'PERMIT_FEE', -- REVIEW_FEE depends on open question O10

    amount               numeric(18,2) NOT NULL,
    paid_amount          numeric(18,2) NOT NULL DEFAULT 0,
    refunded_amount      numeric(18,2) NOT NULL DEFAULT 0,
    currency             text NOT NULL DEFAULT 'UZS',

    -- Benefit applied at calculation time, resolution VMQ 278 clauses 9-11.
    -- The rate is kept here as well so an invoice can be explained without
    -- reopening the calculation snapshot.
    benefit_category_code text,
    benefit_rate         numeric(5,4),

    status               text NOT NULL DEFAULT 'CREATED',
    issued_at            timestamptz NOT NULL DEFAULT now(),
    due_at               timestamptz NOT NULL,               -- 10 days, scenario C9 clause 9.1
    paid_at              timestamptz,
    paid_source          text,                               -- PROVIDER | BANK | MANUAL

    -- Manual marking, scenario C9 clause 9.4: maker-checker plus a bank document.
    manual_maker_id      uuid REFERENCES iam.user_account(id),
    manual_checker_id    uuid REFERENCES iam.user_account(id),
    manual_document_key  text,
    manual_risk_code     text,                               -- RI-01

    legacy_id            bigint,
    legacy_table         text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           uuid,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    updated_by           uuid,

    CONSTRAINT invoice_number_unique UNIQUE (number),

    CONSTRAINT invoice_type_known CHECK (invoice_type IN ('PERMIT_FEE', 'REVIEW_FEE')),

    CONSTRAINT invoice_status_known CHECK (
        status IN (
            'CREATED', 'PENDING', 'PAID', 'RECONCILED',
            'FAILED', 'EXPIRED', 'REFUNDED', 'PARTIALLY_REFUNDED', 'CANCELLED'
        )
    ),

    CONSTRAINT invoice_paid_source_known CHECK (
        paid_source IS NULL OR paid_source IN ('PROVIDER', 'BANK', 'MANUAL')
    ),

    CONSTRAINT invoice_amount_non_negative CHECK (amount >= 0),
    -- paid_amount is not capped by amount on purpose: an overpayment is
    -- recorded as it arrived and then refunded through scenario C14.
    CONSTRAINT invoice_paid_amount_non_negative CHECK (paid_amount >= 0),
    CONSTRAINT invoice_refunded_amount_non_negative CHECK (refunded_amount >= 0),
    CONSTRAINT invoice_currency_known CHECK (currency = 'UZS'),

    CONSTRAINT invoice_benefit_rate_range CHECK (
        benefit_rate IS NULL OR (benefit_rate >= 0 AND benefit_rate <= 1)
    ),

    CONSTRAINT invoice_due_after_issue CHECK (due_at >= issued_at),

    -- The invariant of the phase: PAID always names its confirmation.
    CONSTRAINT invoice_paid_needs_confirmation CHECK (
        status <> 'PAID' OR (paid_at IS NOT NULL AND paid_source IS NOT NULL)
    ),

    -- The exception is allowed, but never quietly: two different people and a
    -- bank document, otherwise the row cannot exist.
    CONSTRAINT invoice_manual_needs_maker_checker CHECK (
        paid_source IS DISTINCT FROM 'MANUAL'
        OR (
            manual_maker_id IS NOT NULL
            AND manual_checker_id IS NOT NULL
            AND manual_maker_id <> manual_checker_id
            AND manual_document_key IS NOT NULL
        )
    )
);

COMMENT ON TABLE pay.invoice IS
    'Invoice built from the calculation snapshot. 100 % prepayment, partial payment is not accepted.';
COMMENT ON COLUMN pay.invoice.paid_source IS
    'Where the PAID confirmation came from. MANUAL is the exceptional path of scenario C9 clause 9.4 '
    'and requires maker, checker and a bank document.';

-- Prosecutor filter by territory and amount.
-- Transferred from architecture/database.md, clause 7.
CREATE INDEX invoice_by_amount ON pay.invoice (territory_code, amount);

CREATE INDEX invoice_by_application ON pay.invoice (application_id);
CREATE INDEX invoice_by_applicant   ON pay.invoice (applicant_id, issued_at DESC);
CREATE INDEX invoice_overdue        ON pay.invoice (due_at)
    WHERE status IN ('CREATED', 'PENDING');
CREATE INDEX invoice_by_legacy      ON pay.invoice (legacy_table, legacy_id)
    WHERE legacy_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- pay.payment_intent
--
-- One attempt to pay an invoice through one provider. A failed attempt does not
-- close the invoice: the applicant picks another provider and a new intent is
-- created (scenario C9, clause 9.3).
-- ---------------------------------------------------------------------------

CREATE TABLE pay.payment_intent (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id       uuid NOT NULL REFERENCES pay.invoice(id),
    provider_code    text NOT NULL,

    amount           numeric(18,2) NOT NULL,
    currency         text NOT NULL DEFAULT 'UZS',
    status           text NOT NULL DEFAULT 'CREATED',

    idempotency_key  text NOT NULL,
    external_ref     text,                              -- order id on the provider side
    return_url       text,
    expires_at       timestamptz,
    failure_code     text,
    failure_message  text,

    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by       uuid,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       uuid,

    CONSTRAINT payment_intent_idempotency_key_unique UNIQUE (idempotency_key),

    -- A fifth provider is added by inserting rows, not by changing core code.
    CONSTRAINT payment_intent_provider_known CHECK (
        provider_code IN ('PAYME', 'CLICK', 'UZUM', 'PAYNET')
    ),

    CONSTRAINT payment_intent_status_known CHECK (
        status IN ('CREATED', 'PENDING', 'PAID', 'FAILED', 'EXPIRED', 'CANCELLED')
    ),

    CONSTRAINT payment_intent_amount_positive CHECK (amount > 0),
    CONSTRAINT payment_intent_currency_known CHECK (currency = 'UZS')
);

COMMENT ON TABLE pay.payment_intent IS
    'One payment attempt against one provider. idempotency_key is the key of scenario C9 clause 9.2.';

CREATE UNIQUE INDEX payment_intent_by_external_ref
    ON pay.payment_intent (provider_code, external_ref)
    WHERE external_ref IS NOT NULL;
CREATE INDEX payment_intent_by_invoice ON pay.payment_intent (invoice_id, created_at DESC);


-- ---------------------------------------------------------------------------
-- pay.provider_transaction
--
-- A confirmation received from a provider. reconciliation_id is declared here
-- and its foreign key is added after pay.reconciliation exists: the two tables
-- point at each other.
-- ---------------------------------------------------------------------------

CREATE TABLE pay.provider_transaction (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_intent_id   uuid NOT NULL REFERENCES pay.payment_intent(id),
    invoice_id          uuid NOT NULL REFERENCES pay.invoice(id),   -- denormalised for reconciliation
    provider_code       text NOT NULL,
    external_id         text NOT NULL,                  -- transaction id on the provider side

    amount              numeric(18,2) NOT NULL,
    fee_amount          numeric(18,2) NOT NULL DEFAULT 0,
    currency            text NOT NULL DEFAULT 'UZS',

    status              text NOT NULL DEFAULT 'CREATED',
    occurred_at         timestamptz NOT NULL,           -- time reported by the provider
    received_at         timestamptz NOT NULL DEFAULT now(),

    -- A webhook is trusted only after its signature verifies.
    signature_verified  boolean NOT NULL DEFAULT false,
    signature_header    text,
    idempotency_key     text NOT NULL,
    raw_payload         jsonb NOT NULL DEFAULT '{}'::jsonb,

    reconciliation_id   uuid,                           -- foreign key added below
    mismatch_code       text,                           -- ERR-PAY-003 when the amount disagrees

    legacy_id           bigint,
    legacy_table        text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          uuid,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          uuid,

    -- A repeated webhook cannot create a second transaction.
    CONSTRAINT provider_transaction_external_unique UNIQUE (provider_code, external_id),
    CONSTRAINT provider_transaction_idempotency_key_unique UNIQUE (idempotency_key),

    CONSTRAINT provider_transaction_provider_known CHECK (
        provider_code IN ('PAYME', 'CLICK', 'UZUM', 'PAYNET')
    ),

    CONSTRAINT provider_transaction_status_known CHECK (
        status IN (
            'CREATED', 'PENDING', 'PAID', 'FAILED', 'EXPIRED',
            'REFUNDED', 'PARTIALLY_REFUNDED', 'CANCELLED'
        )
    ),

    CONSTRAINT provider_transaction_amount_positive CHECK (amount > 0),
    CONSTRAINT provider_transaction_fee_non_negative CHECK (fee_amount >= 0),
    CONSTRAINT provider_transaction_currency_known CHECK (currency = 'UZS'),

    -- The invariant again, one level lower: an unsigned webhook can be stored
    -- for the audit trail, but it can never carry the PAID state.
    CONSTRAINT provider_transaction_paid_needs_signature CHECK (
        status <> 'PAID' OR signature_verified
    )
);

COMMENT ON TABLE pay.provider_transaction IS
    'Payment confirmation from a provider. PAID requires a verified signature: scenario C9, clause 5.';
COMMENT ON COLUMN pay.provider_transaction.reconciliation_id IS
    'Set once the transaction is matched against a bank statement line. NULL means unreconciled.';

-- Reconciliation lookups.
-- Transferred from architecture/database.md, clause 7.
CREATE INDEX txn_by_external      ON pay.provider_transaction (external_id);
CREATE INDEX txn_unreconciled     ON pay.provider_transaction (created_at)
    WHERE status = 'PAID' AND reconciliation_id IS NULL;

CREATE INDEX txn_by_invoice       ON pay.provider_transaction (invoice_id, occurred_at DESC);
CREATE INDEX txn_by_intent        ON pay.provider_transaction (payment_intent_id);
CREATE INDEX txn_by_legacy        ON pay.provider_transaction (legacy_table, legacy_id)
    WHERE legacy_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- pay.bank_statement
--
-- A bank statement, loaded as a file or pulled through the bank API
-- (scenario C10, clause 2).
-- ---------------------------------------------------------------------------

CREATE TABLE pay.bank_statement (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_number   text NOT NULL,
    bank_code        text,                              -- MFO of the servicing bank
    statement_date   date NOT NULL,
    currency         text NOT NULL DEFAULT 'UZS',

    opening_balance  numeric(18,2),
    closing_balance  numeric(18,2),
    total_credit     numeric(18,2) NOT NULL DEFAULT 0,
    total_debit      numeric(18,2) NOT NULL DEFAULT 0,
    line_count       int NOT NULL DEFAULT 0,

    source           text NOT NULL,                     -- FILE | API
    file_key         text,
    file_hash        text,

    status           text NOT NULL DEFAULT 'LOADED',
    loaded_at        timestamptz NOT NULL DEFAULT now(),
    reconciled_at    timestamptz,

    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by       uuid,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       uuid,

    CONSTRAINT bank_statement_unique UNIQUE (account_number, statement_date),

    CONSTRAINT bank_statement_source_known CHECK (source IN ('FILE', 'API')),

    CONSTRAINT bank_statement_status_known CHECK (
        status IN ('LOADED', 'RECONCILING', 'RECONCILED', 'FAILED')
    ),

    CONSTRAINT bank_statement_currency_known CHECK (currency = 'UZS'),
    CONSTRAINT bank_statement_totals_non_negative CHECK (total_credit >= 0 AND total_debit >= 0),
    CONSTRAINT bank_statement_line_count_non_negative CHECK (line_count >= 0)
);

COMMENT ON TABLE pay.bank_statement IS
    'Bank statement for one account and one day. Loaded from a file or through the bank API.';

CREATE INDEX bank_statement_by_date ON pay.bank_statement (statement_date DESC);


-- ---------------------------------------------------------------------------
-- pay.bank_statement_line
--
-- One line of a statement. statement_date is denormalised from the header
-- because the reconciliation index searches by date and amount without
-- touching the header.
-- ---------------------------------------------------------------------------

CREATE TABLE pay.bank_statement_line (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    statement_id          uuid NOT NULL REFERENCES pay.bank_statement(id),
    statement_date        date NOT NULL,                -- denormalised from bank_statement
    line_no               int NOT NULL,

    direction             text NOT NULL,                -- CREDIT | DEBIT
    amount                numeric(18,2) NOT NULL,
    currency              text NOT NULL DEFAULT 'UZS',

    operation_date        date,
    value_date            date,
    document_number       text,
    external_ref          text,                         -- unique reference on the bank side

    payer_name            text,
    payer_account         text,
    payer_tin             text,
    payee_account         text,
    purpose               text,                         -- payment purpose text

    matched_transaction_id uuid REFERENCES pay.provider_transaction(id),
    status                text NOT NULL DEFAULT 'UNMATCHED',

    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            uuid,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            uuid,

    CONSTRAINT bank_statement_line_no_unique UNIQUE (statement_id, line_no),

    CONSTRAINT bank_statement_line_direction_known CHECK (direction IN ('CREDIT', 'DEBIT')),

    CONSTRAINT bank_statement_line_status_known CHECK (
        status IN ('UNMATCHED', 'MATCHED', 'UNKNOWN', 'IGNORED')
    ),

    CONSTRAINT bank_statement_line_amount_positive CHECK (amount > 0),
    CONSTRAINT bank_statement_line_currency_known CHECK (currency = 'UZS'),
    CONSTRAINT bank_statement_line_no_positive CHECK (line_no > 0),

    CONSTRAINT bank_statement_line_matched_has_transaction CHECK (
        status <> 'MATCHED' OR matched_transaction_id IS NOT NULL
    )
);

COMMENT ON TABLE pay.bank_statement_line IS
    'Statement line. Status UNKNOWN is the "unknown payments" list of scenario C10, clause 10.1: '
    'money arrived at the bank with no matching transaction in the system.';

-- Reconciliation lookup by day and amount.
-- Transferred from architecture/database.md, clause 7.
CREATE INDEX stmt_line_by_amount  ON pay.bank_statement_line (statement_date, amount);

CREATE INDEX stmt_line_by_statement ON pay.bank_statement_line (statement_id, line_no);
CREATE INDEX stmt_line_unmatched    ON pay.bank_statement_line (statement_date)
    WHERE status = 'UNMATCHED';


-- ---------------------------------------------------------------------------
-- pay.reconciliation
--
-- Result of matching a provider transaction against a bank statement line
-- (scenario C10, clauses 3 to 6). A discrepancy is not deleted, it is closed
-- with a note or with a correcting document.
-- ---------------------------------------------------------------------------

CREATE TABLE pay.reconciliation (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date               date NOT NULL,
    provider_transaction_id uuid REFERENCES pay.provider_transaction(id),
    statement_id           uuid REFERENCES pay.bank_statement(id),
    statement_line_id      uuid REFERENCES pay.bank_statement_line(id),
    invoice_id             uuid REFERENCES pay.invoice(id),

    result                 text NOT NULL,
    system_amount          numeric(18,2),
    bank_amount            numeric(18,2),
    discrepancy_amount     numeric(18,2) NOT NULL DEFAULT 0,

    risk_indicator_code    text,                        -- RI-10 when money exists in the system but not in the bank
    status                 text NOT NULL DEFAULT 'OPEN',
    note                   text,
    correction_document_key text,
    assignee_id            uuid REFERENCES iam.user_account(id),
    resolved_at            timestamptz,
    resolved_by            uuid REFERENCES iam.user_account(id),

    created_at             timestamptz NOT NULL DEFAULT now(),
    created_by             uuid,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    updated_by             uuid,

    CONSTRAINT reconciliation_result_known CHECK (
        result IN (
            'MATCHED',           -- both sides agree, marked RECONCILED
            'AMOUNT_MISMATCH',   -- ERR-PAY-003
            'MISSING_IN_BANK',   -- in the system, not in the statement: RI-10
            'MISSING_IN_SYSTEM', -- in the statement, not in the system: unknown payment
            'DUPLICATE'
        )
    ),

    CONSTRAINT reconciliation_status_known CHECK (
        status IN ('OPEN', 'IN_PROGRESS', 'CLOSED')
    ),

    CONSTRAINT reconciliation_amounts_non_negative CHECK (
        (system_amount IS NULL OR system_amount >= 0)
        AND (bank_amount IS NULL OR bank_amount >= 0)
        AND discrepancy_amount >= 0
    ),

    -- A match has both sides. Anything else has at least one.
    CONSTRAINT reconciliation_matched_has_both_sides CHECK (
        result <> 'MATCHED'
        OR (provider_transaction_id IS NOT NULL AND statement_line_id IS NOT NULL)
    ),

    CONSTRAINT reconciliation_has_a_side CHECK (
        provider_transaction_id IS NOT NULL OR statement_line_id IS NOT NULL
    ),

    CONSTRAINT reconciliation_closed_has_outcome CHECK (
        status <> 'CLOSED'
        OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL)
    )
);

COMMENT ON TABLE pay.reconciliation IS
    'Daily reconciliation of provider transactions against bank statement lines.';

-- Closes the cycle between provider_transaction and reconciliation.
ALTER TABLE pay.provider_transaction
    ADD CONSTRAINT provider_transaction_reconciliation_fk
    FOREIGN KEY (reconciliation_id) REFERENCES pay.reconciliation(id);

CREATE INDEX reconciliation_by_run    ON pay.reconciliation (run_date DESC, result);
CREATE INDEX reconciliation_open      ON pay.reconciliation (run_date DESC)
    WHERE status <> 'CLOSED';
CREATE INDEX reconciliation_by_txn    ON pay.reconciliation (provider_transaction_id)
    WHERE provider_transaction_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- pay.allocation
--
-- Allocation ledger, scenario C10 clause 1: a payment is split 50/50 between
-- the forest enterprise and the budget, per resolution VMQ 278.
--
-- ROUNDING. Splitting an odd amount in half cannot be done without deciding
-- where the odd kopeck goes. That decision is a policy and lives in the
-- application: it knows the ratio version, the recipient order and the
-- remainder rule. This table stores amounts that have already been distributed
-- and never divides anything itself. The invariant the application must keep is
-- that the sum of the active allocation rows of a transaction equals the
-- transaction amount to the kopeck, with no rounding loss.
--
-- A change of the ratio does not rewrite history (clause 10.3): the old rows
-- are superseded, not updated.
-- ---------------------------------------------------------------------------

CREATE TABLE pay.allocation (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_transaction_id uuid NOT NULL REFERENCES pay.provider_transaction(id),
    invoice_id             uuid NOT NULL REFERENCES pay.invoice(id),

    recipient_type         text NOT NULL,               -- FOREST_ENTERPRISE | BUDGET
    recipient_name         text NOT NULL,
    recipient_account      text NOT NULL,               -- settlement account
    recipient_bank_code    text,                        -- MFO
    recipient_tin          text,
    recipient_org_id       uuid REFERENCES iam.organization(id),

    share_percent          numeric(5,2) NOT NULL,       -- 50.00 under the current rule
    ratio_version          text NOT NULL,               -- which ratio produced this row
    amount                 numeric(18,2) NOT NULL,      -- already distributed, see the note above
    currency               text NOT NULL DEFAULT 'UZS',

    status                 text NOT NULL DEFAULT 'PLANNED',
    allocated_at           timestamptz NOT NULL DEFAULT now(),
    transferred_at         timestamptz,
    bank_reference         text,
    superseded_by_id       uuid REFERENCES pay.allocation(id),

    created_at             timestamptz NOT NULL DEFAULT now(),
    created_by             uuid,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    updated_by             uuid,

    CONSTRAINT allocation_recipient_type_known CHECK (
        recipient_type IN ('FOREST_ENTERPRISE', 'BUDGET')
    ),

    CONSTRAINT allocation_status_known CHECK (
        status IN ('PLANNED', 'TRANSFERRED', 'FAILED', 'REVERSED', 'SUPERSEDED')
    ),

    CONSTRAINT allocation_share_range CHECK (share_percent > 0 AND share_percent <= 100),
    CONSTRAINT allocation_amount_non_negative CHECK (amount >= 0),
    CONSTRAINT allocation_currency_known CHECK (currency = 'UZS'),

    CONSTRAINT allocation_not_superseded_by_self CHECK (
        superseded_by_id IS NULL OR superseded_by_id <> id
    ),

    CONSTRAINT allocation_transferred_has_time CHECK (
        status <> 'TRANSFERRED' OR transferred_at IS NOT NULL
    )
);

COMMENT ON TABLE pay.allocation IS
    'Allocation ledger. Rows hold already distributed amounts; the rounding policy for an odd kopeck '
    'is implemented in the application, which must keep the sum of active rows equal to the '
    'transaction amount.';
COMMENT ON COLUMN pay.allocation.superseded_by_id IS
    'Set when the ratio is recalculated. History is kept, not overwritten: scenario C10, clause 10.3.';

CREATE INDEX allocation_by_transaction ON pay.allocation (provider_transaction_id, recipient_type);
CREATE INDEX allocation_by_invoice     ON pay.allocation (invoice_id);
CREATE INDEX allocation_active         ON pay.allocation (provider_transaction_id)
    WHERE superseded_by_id IS NULL;
CREATE INDEX allocation_pending_transfer ON pay.allocation (allocated_at)
    WHERE status = 'PLANNED';


-- ---------------------------------------------------------------------------
-- pay.refund
--
-- Refund, scenario C14. The ground, the formula, the amount, the status and an
-- SLA of 20 working days. A deviation from the formula is blocked and raises
-- RI-11; a breach of the SLA raises RI-07.
-- ---------------------------------------------------------------------------

CREATE TABLE pay.refund (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id          uuid NOT NULL REFERENCES app.application(id),
    permit_id               uuid REFERENCES permit.permit(id),
    invoice_id              uuid REFERENCES pay.invoice(id),
    provider_transaction_id uuid REFERENCES pay.provider_transaction(id),
    applicant_id            uuid NOT NULL REFERENCES iam.applicant(id),
    territory_code          text NOT NULL,

    -- Ground, resolution VMQ 278 clauses 9 to 11.
    reason_code             text NOT NULL,
    legal_base              text NOT NULL,
    supporting_document_key text,
    benefit_category_code   text,

    -- Formula and its inputs, kept so any refund can be explained afterwards.
    formula                 text NOT NULL
        DEFAULT 'paid_amount * unused_eligible_days / paid_period_days',
    paid_amount             numeric(18,2) NOT NULL,
    paid_period             daterange NOT NULL,
    unused_period           daterange,
    paid_period_days        int NOT NULL,
    unused_eligible_days    int NOT NULL DEFAULT 0,

    calculated_amount       numeric(18,2) NOT NULL,     -- what the formula produced
    approved_amount         numeric(18,2),              -- what the head approved
    transferred_amount      numeric(18,2),              -- what the bank actually sent
    currency                text NOT NULL DEFAULT 'UZS',

    status                  text NOT NULL DEFAULT 'REQUESTED',

    -- Maker-checker: the accountant calculates, the head approves.
    maker_id                uuid REFERENCES iam.user_account(id),
    checker_id              uuid REFERENCES iam.user_account(id),

    -- SLA of 20 working days, scenario C14 clause 6.
    requested_at            timestamptz NOT NULL DEFAULT now(),
    sla_working_days        int NOT NULL DEFAULT 20,
    sla_due_at              timestamptz NOT NULL,
    completed_at            timestamptz,
    sla_breached            boolean NOT NULL DEFAULT false,

    bank_account            text,
    bank_code               text,
    bank_reference          text,
    risk_indicator_code     text,                       -- RI-07 on an SLA breach, RI-11 outside the formula
    note                    text,

    created_at              timestamptz NOT NULL DEFAULT now(),
    created_by              uuid,
    updated_at              timestamptz NOT NULL DEFAULT now(),
    updated_by              uuid,

    CONSTRAINT refund_reason_code_known CHECK (
        reason_code IN (
            'PERMIT_REVOKED',    -- the permit was revoked
            'UNUSED_PERIOD',     -- part of the paid period was never used
            'OVERPAYMENT',       -- more money arrived than the invoice asked for
            'BENEFIT_APPLIED',   -- a benefit category was confirmed after payment
            'OTHER'              -- note is mandatory, enforced below
        )
    ),

    CONSTRAINT refund_status_known CHECK (
        status IN (
            'REQUESTED', 'UNDER_REVIEW', 'APPROVED', 'REJECTED',
            'TRANSFERRED', 'FAILED', 'CANCELLED'
        )
    ),

    CONSTRAINT refund_amounts_non_negative CHECK (
        paid_amount >= 0
        AND calculated_amount >= 0
        AND (approved_amount IS NULL OR approved_amount >= 0)
        AND (transferred_amount IS NULL OR transferred_amount >= 0)
    ),

    CONSTRAINT refund_not_more_than_paid CHECK (
        calculated_amount <= paid_amount
        AND (approved_amount IS NULL OR approved_amount <= paid_amount)
        AND (transferred_amount IS NULL OR transferred_amount <= paid_amount)
    ),

    CONSTRAINT refund_currency_known CHECK (currency = 'UZS'),

    CONSTRAINT refund_days_sane CHECK (
        paid_period_days > 0
        AND unused_eligible_days >= 0
        AND unused_eligible_days <= paid_period_days
    ),

    CONSTRAINT refund_sla_positive CHECK (sla_working_days > 0),
    CONSTRAINT refund_sla_due_after_request CHECK (sla_due_at >= requested_at),

    CONSTRAINT refund_other_needs_note CHECK (reason_code <> 'OTHER' OR note IS NOT NULL),

    -- Maker-checker, scenario C14 clause 4: two different people.
    CONSTRAINT refund_needs_maker_checker CHECK (
        status NOT IN ('APPROVED', 'TRANSFERRED')
        OR (
            maker_id IS NOT NULL
            AND checker_id IS NOT NULL
            AND maker_id <> checker_id
            AND approved_amount IS NOT NULL
        )
    ),

    CONSTRAINT refund_transferred_has_reference CHECK (
        status <> 'TRANSFERRED'
        OR (transferred_amount IS NOT NULL AND completed_at IS NOT NULL)
    )
);

COMMENT ON TABLE pay.refund IS
    'Refund under scenario C14. calculated_amount is what the formula produced; a deviation from it '
    'is blocked by the application and raises RI-11.';
COMMENT ON COLUMN pay.refund.sla_due_at IS
    'Deadline of 20 working days. Computed against the working calendar by the application, because '
    'the database has no calendar of holidays.';

CREATE INDEX refund_by_application ON pay.refund (application_id);
CREATE INDEX refund_by_applicant   ON pay.refund (applicant_id, requested_at DESC);
CREATE INDEX refund_by_invoice     ON pay.refund (invoice_id) WHERE invoice_id IS NOT NULL;
CREATE INDEX refund_open_sla       ON pay.refund (sla_due_at)
    WHERE status IN ('REQUESTED', 'UNDER_REVIEW', 'APPROVED');
CREATE INDEX refund_by_territory   ON pay.refund (territory_code, status, requested_at DESC);
