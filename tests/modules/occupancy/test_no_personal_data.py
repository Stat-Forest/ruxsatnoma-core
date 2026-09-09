"""Hard requirement: who holds the permit must not appear in this response —
the applicant learns that a period is taken and how much room is left,
nothing else."""

from datetime import date
from decimal import Decimal

from tests.modules.occupancy.conftest import issue_permit, occupancy_url
from tests.modules.permits.conftest import HOLDER_NAME

# Keys that would leak WHO holds a permit, or point at the row that would —
# none of `occupancy`'s own schema fields use any of these names.
_FORBIDDEN_KEYS = {
    "applicant_id",
    "applicant",
    "holder_name",
    "holder_pinfl",
    "pinfl",
    "snapshot",
    "organization_id",
    "series",
    "number",
    "qr_token",
}


def _walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, sub in value.items():
            keys.add(key)
            keys |= _walk_keys(sub)
    elif isinstance(value, list):
        for item in value:
            keys |= _walk_keys(item)
    return keys


async def test_no_applicant_identity_anywhere_in_the_payload(
    db, published_contour, leshoz, grazing_activity_id, applicant_client
):
    await issue_permit(
        db,
        contour=published_contour,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        period_from=date(2028, 6, 1),
        period_to=date(2028, 6, 30),
        sb_load=Decimal("40"),
    )
    await db.commit()

    resp = await applicant_client.get(
        occupancy_url(
            published_contour.id, grazing_activity_id, date(2028, 6, 1), date(2028, 6, 30)
        )
    )

    assert resp.status_code == 200, resp.text
    raw = resp.text
    # The holder's own printed name and PINFL — the exact facts a permit's
    # `snapshot` carries (`permits.service._snapshot`) — must not be
    # reconstructable from the raw response text at all, not merely absent
    # from a checked subset of fields.
    assert HOLDER_NAME not in raw

    body = resp.json()
    assert _walk_keys(body) & _FORBIDDEN_KEYS == set()
