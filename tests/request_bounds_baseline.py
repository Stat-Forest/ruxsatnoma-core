"""Baseline of request-body fields still missing an upper bound (stage 17, QA run 01).

This set only SHRINKS. Every later stage-17 task that bounds a field deletes
that field's key here in the same commit, in the module it owns; Task 8
deletes this file once the set is empty. Do not add a key that was never
here — a newly introduced unbounded field is a regression
`tests/test_request_bounds.py` must catch, not a baseline entry to grow.

Measured 2026-09-24 against branch base c307922 (stage 16 merged): 474
request-body fields checked, 201 without a bound. The stage's own research
walk (a throwaway script, same idea as the convention test) counted 224
before stage 16 merged; stage 16 added some request schemas and also
bounded some fields the walk had flagged, so the two counts are not
expected to match exactly.
"""

BASELINE: frozenset[str] = frozenset(
    {
        "ActivitySeasonIn.min_term_days",
        "ActivitySeasonPatch.min_term_days",
        "AddRepresentationIn.signed_challenge",
        "AttachLegalIn.name",
        "AttachLegalIn.signed_challenge",
        "CalculationIn.benefit_code",
        "CalculationIn.quantity",
        "CertificateBindIn.pkcs7",
        "CompleteRegistrationIn.address",
        "CompleteRegistrationIn.otp_token",
        "CompleteRegistrationIn.phone",
        "ConsentsIn.offer",
        "ConsentsIn.privacy_policy",
        "ContactUpdateIn.otp_token",
        "ContactUpdateIn.phone",
        "EimzoLoginIn.signed_challenge",
        "EimzoTimestampIn.pkcs7",
        "ExportCreate.status",
        "FaqIn.sort_order",
        "FaqPatch.sort_order",
        "ForestTicketIn.restrictions",
        "LoginIn.login",
        "LoginIn.password",
        "ManualConfirmationIn.amount",
        "ManualConfirmationRejectIn.reason",
        "MfaIn.code",
        "MfaIn.mfa_token",
        "NormIn.capacity",
        "NormIn.yield_c_per_ha",
        "NormPatch.capacity",
        "NormPatch.yield_c_per_ha",
        "OtpRequestIn.target",
        "OtpVerifyIn.code",
        "OtpVerifyIn.target",
        "PasswordChangeIn.new_password",
        "PasswordChangeIn.old_password",
        "PasswordForgotResetIn.new_password",
        "PaymentRecipientIn.fixed_amount",
        "PaymentRecipientIn.note",
        "PaymentRecipientIn.payme_account_id",
        "PaymentRecipientIn.sort_order",
        "PaymentRecipientPatch.fixed_amount",
        "PaymentRecipientPatch.note",
        "PaymentRecipientPatch.payme_account_id",
        "PaymentRecipientPatch.sort_order",
        "PermitSignIn.pkcs7",
        "PublicEstimateIn.quantity",
        "ReconciliationResolveIn.comment",
        "RefundApproveIn.comment",
        "RefundComponentIn.amount",
        "RefundRequestIn.comment",
        "RefundSubmitDecisionIn.comment",
        "RefundSubmitDecisionIn.components",
        "RefundSubmitDecisionIn.final_amount",
        "Rotation.rest_years",
        "Rotation.rest_years[]",
        "RuleParameterIn.unit",
        "RuleParameterIn.value",
        "RuleParameterPatch.basis",
        "RuleParameterPatch.unit",
        "RuleParameterPatch.value",
        "SavedFilterIn.params",
        "SavedFilterIn.shared",
        "SavedFilterIn.shared.*",
        "SavedFilterIn.shared.*[]",
        "SavedFilterPatch.params",
        "SavedFilterPatch.shared",
        "SavedFilterPatch.shared.*",
        "SavedFilterPatch.shared.*[]",
        "Season.windows",
        "SettingIn.value",
        "TariffIn.benefit_modifiers",
        "TariffIn.benefit_modifiers.*",
        "TariffIn.coefficient",
        "TariffPatch.basis",
        "TariffPatch.benefit_modifiers",
        "TariffPatch.benefit_modifiers.*",
        "TariffPatch.coefficient",
        "app__modules__permits__schemas__DecisionIn.pkcs7",
    }
)
