"""Legal grounds for a permit's lifecycle transitions — plan
`03.11b-permits-lifecycle` ruling 5, С13's «основание обязательно… +
классификатор причины».

Pure by design, in the same spirit as `signers.py`: no HTTP, no direct
`classifier_items` query — reads run through `admin.repo`, never a
re-implemented query here (backend/CLAUDE.md § Reference data) — and no
writer of `permits.status` of its own; `assert_applicable` only answers
whether a decision MAY be taken, never takes one.

The classifier itself, `permit_status_reasons`, did not exist before this
stage: `permit_status_history.reason_item_id` has FK'd the whole of
`classifier_items` since migration 0019, with nothing under it to point at.
Migration 0023 seeds it and its seven items, PS-01…PS-07 (see that file for
the table). The FK cannot tell a `rejection_reasons` row apart from one of
these — an application's RJ-* code fits the same column — so
`assert_applicable` is what refuses one, not the database.

`assert_applicable` returns the resolved item so the caller can put its CODE
in the signed decision and the append-only `permit_status_history` row
without a second lookup.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.time import business_today
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import ClassifierItem

CLASSIFIER_CODE = "permit_status_reasons"

SUSPEND = "suspend"
RESUME = "resume"
REVOKE = "revoke"
ACTS: tuple[str, ...] = (SUSPEND, RESUME, REVOKE)

# The one item whose free-text explanation is not optional — the same rule
# RJ-15 «Бошқа (изоҳ мажбурий)» carries for an application's rejection.
EXPLANATION_REQUIRED = "PS-07"


async def assert_applicable(
    db: AsyncSession, *, reason_item_id: uuid.UUID, act: str, legal_basis: str | None
) -> ClassifierItem:
    """The ground `act` is being decided on, or `ERR-VAL-001` naming what is wrong.

    Read through `admin.repo`, never a query of `classifier_items` from here
    (backend/CLAUDE.md § Reference data). Returns the item so the caller can put
    its CODE in the signed statement without a second lookup.
    """
    assert act in ACTS, f"unknown act: {act}"  # a programming error, not user input
    item = await admin_repo.get_classifier_item(db, reason_item_id)
    if item is None:
        raise err("ERR-VAL-001", details={"reason": "reason_not_found"})
    classifier = await admin_repo.get_classifier_by_code(db, CLASSIFIER_CODE)
    if classifier is None or item.classifier_id != classifier.id:
        raise err("ERR-VAL-001", details={"reason": "reason_wrong_classifier"})
    if item.status != "active":
        raise err("ERR-VAL-001", details={"reason": "reason_archived"})
    today = business_today()
    if item.valid_from > today or (item.valid_to is not None and item.valid_to < today):
        raise err("ERR-VAL-001", details={"reason": "reason_out_of_validity"})
    if act not in _kinds(item):
        raise err("ERR-VAL-001", details={"reason": "reason_not_applicable"})
    if item.code == EXPLANATION_REQUIRED and not (legal_basis or "").strip():
        raise err("ERR-VAL-001", details={"reason": "legal_basis_required"})
    return item


def _kinds(item: ClassifierItem) -> frozenset[str]:
    """`props.kinds`, defensively. An administrator can edit a classifier item
    through `/admin/classifiers`, so a row whose `props` is not a dict, or
    whose `kinds` is not a list of strings, must refuse the act rather than
    raise a 500 on a legitimate request."""
    props = item.props if isinstance(item.props, dict) else {}
    raw = props.get("kinds")
    return frozenset(v for v in raw if isinstance(v, str)) if isinstance(raw, list) else frozenset()
