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

    `None` for both the recipient purpose and an unknown one — the two are told
    apart by `is_known`, which is the question a caller must ask FIRST. One dict
    answering both keeps them from drifting apart the way a second, separately
    maintained set of known purposes would.
    """
    return PURPOSE_ROLES.get(purpose)


def is_known(purpose: str) -> bool:
    """Whether this purpose is a signature line of the permit at all. `False`
    is a refusal, not a pass: see the module docstring on failing closed."""
    return purpose in PURPOSE_ROLES
