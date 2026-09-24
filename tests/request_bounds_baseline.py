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
        "RuleParameterIn.value",
        "RuleParameterPatch.value",
        "SettingIn.value",
    }
)
