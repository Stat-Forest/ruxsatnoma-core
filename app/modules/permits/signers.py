"""Who may sign which line of the permit — ruling 4, as data rather than an `if`.

Pure by design: no session, no permit, no HTTP. `tz/13`'s form 1-ilova carries
four signature lines and this file is the whole of the map between a signature
PURPOSE and the person entitled to produce it, so the rule can be read, reviewed
and tested on its own.

Stage 3.8 built the signature machinery and said, in `require_complete`'s own
docstring, what it deliberately did not build: it matches purpose STRINGS only,
"nothing here checks that the person who signed `permit_head` actually holds
that role — one user with one certificate can currently sign every required
purpose on their own and this still reports complete". That is the hole this map
closes, and `permits.service.add_signature` is the one place that consults it.

**The map fails closed.** The required set itself lives in the admin-editable
`permit_required_signatures` system setting (ruling 7), so an operator can add a
purpose to it at any time. A purpose that is required but has no entry HERE is
refused, never allowed: the alternative is a typo silently opening a signature
slot that anybody holding `permits.sign` could fill, on a legal document.

Every role code below was read out of `0003_auth.py`, never out of plan prose —
a role name from spec prose is not a `roles.code` (lesson: there is no `rahbar`
code; «Раҳбар», the approver of `tz/03`'s matrix, is `executor_head`).
"""

# The purpose the HOLDER signs. It maps to no role on purpose: `applicant` is
# held by every citizen in the country, so holding it proves nothing about THIS
# permit. The proof is owning — or effectively representing — the applicant of
# the permit's own application, which only the service can check because only it
# has the permit in hand. `sign()` then re-proves the certificate is that
# person's own by PINFL/STIR, which is a different question again.
RECIPIENT_PURPOSE = "permit_recipient"

# `tz/13` requisites 20-23, in the order the form prints them. `None` marks a
# purpose that is known but not role-based; a purpose ABSENT from this dict is
# unknown, and unknown means refused (see the module docstring).
PURPOSE_ROLES: dict[str, str | None] = {
    # 20 «Ваколатли шахс» — the leshoz head, tz/03 role 4.
    "permit_head": "executor_head",
    # 21 «Бош ўрмончи» — the chief forester, a role that exists for this second
    # signature and no other (decision #32).
    "permit_chief_forester": "chief_forester",
    # 22 «Бухгалтер».
    "permit_accountant": "accountant",
    # 23 «Рухсатнома олувчи» — the holder; see RECIPIENT_PURPOSE above.
    RECIPIENT_PURPOSE: None,
}


def required_role(purpose: str) -> str | None:
    """The `roles.code` a signer must hold to produce `purpose`.

    **`None` is a refusal, not a pass** — that is the whole of the fail-closed
    rule. It is returned both for a purpose absent from the map (an operator's
    typo in `permit_required_signatures`) and for `RECIPIENT_PURPOSE`, which is
    known but not role-based; the caller tells them apart by checking the
    recipient purpose FIRST and treating every remaining `None` as unknown. See
    `permits.service._signer_refusal`, which is the only caller.

    There is deliberately no companion `is_known` predicate. Any second way to
    ask "is this a real signature line" is a second thing that can answer
    differently from this one, and a caller could then reach the role comparison
    with `None` on both sides — which is the exact fail-OPEN this map exists to
    prevent.
    """
    return PURPOSE_ROLES.get(purpose)


# --- 3.11b: the signed decision (plan `03.11b-permits-lifecycle` ruling 4) ---
#
# `DECISION_PURPOSE_ROLES` is a SEPARATE map, beside `PURPOSE_ROLES` above and
# deliberately never inside it. `permits.service.add_signature` reads
# `PURPOSE_ROLES` (through `required_role`) to decide who may fill one of
# form 1-ilova's four requisites — a decision purpose added there would become
# signable ON THE PERMIT ITSELF, through the very route `add_signature`
# already exposes, which is exactly the bug ruling 1 exists to avoid (a
# decision signs the ATTEMPT — `permits.decisions.decide`'s own
# `permit_status_history` row — never the permit).
DECISION_PURPOSE = "permit_decision"

DECISION_PURPOSE_ROLES: dict[str, str | None] = {
    DECISION_PURPOSE: "executor_head",
}


def decision_role(purpose: str) -> str | None:
    """The `roles.code` that must sign a permit DECISION (suspend/resume/
    revoke) — `required_role`'s exact fail-closed shape (`None` refuses, never
    passes), over `DECISION_PURPOSE_ROLES` rather than `PURPOSE_ROLES`. See
    `permits.decisions._decision_signer_refusal`, which is the only caller.

    Kept as its own function rather than folded into `required_role` over a
    merged dict, for the same reason the two maps stay apart: merging them
    would let a decision purpose answer a PERMIT-signature lookup (or vice
    versa) the moment a future purpose string collided between the two, and
    the two questions — "who signs form 1-ilova" and "who may decide this
    permit's fate" — must never share an answer by accident.
    """
    return DECISION_PURPOSE_ROLES.get(purpose)
