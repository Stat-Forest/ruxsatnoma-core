"""Who may sign a report — `permits/signers.py`'s exact shape, one purpose.

design/02 § reports: "The rahbar's signature lives in signatures
(purpose=report_approve)". Only one signature line exists on this document
(unlike form 1-ilova's four), but the map is still data rather than an `if`,
for the same reason `permits` keeps one: a purpose absent from this dict is
refused, never allowed — a typo must fail closed, not silently open a slot
`reports.sign` alone would let anybody holding that code fill.
"""

REPORT_APPROVE_PURPOSE = "report_approve"

PURPOSE_ROLES: dict[str, str | None] = {
    REPORT_APPROVE_PURPOSE: "executor_head",
}


def required_role(purpose: str) -> str | None:
    """The `roles.code` a signer must hold to produce `purpose` on a report.

    `None` is a refusal, not a pass (see the module docstring) — returned both
    for an unknown purpose and for one that is known but not role-based (no
    such purpose exists on this document today, unlike `permits`' recipient
    line, but the shape is kept identical so a second purpose can be added the
    same way later).
    """
    return PURPOSE_ROLES.get(purpose)
