"""Stage 13: `GET /certificates/export.xlsx` and `GET /signatures/export.xlsx`
— the caller's own bound certificates and one object's signature list on
paper. Neither is wired to an Excel button by this track (out of its screen
list): `src/pages/admin/profile/certificates/CertificatesSection.tsx` and
`src/pages/permits/PermitSignaturesPanel.tsx` DO read these routes today,
but each shows a small, single-owner/single-object list ("a handful of
keys at most", that file's own comment) rather than a register — track
report flags the correction. Fixtures reused from `test_api.py` (pre-flight
ruling P4: this file adds its own where `test_api.py`'s do not fit, rather
than growing the shared `conftest.py`)."""

import uuid

from app.modules.auth.models import User
from app.modules.signatures import service
from app.modules.signatures.models import Signature
from tests.conftest import assert_export_cut, export_cap, xlsx_rows
from tests.modules.signatures.test_api import (
    DOC,
    _pkcs7,
)
from tests.modules.signatures.test_api import (
    a_signature as a_signature,
)
from tests.modules.signatures.test_api import (
    bound_cert_of_a as bound_cert_of_a,
)
from tests.modules.signatures.test_api import (
    client_a as client_a,
)
from tests.modules.signatures.test_api import (
    client_b as client_b,
)
from tests.modules.signatures.test_api import (
    client_oversight as client_oversight,
)
from tests.modules.signatures.test_api import (
    user_a as user_a,
)
from tests.modules.signatures.test_api import (
    user_b as user_b,
)

CERTIFICATES = "/api/v1/certificates/export.xlsx"
SIGNATURES = "/api/v1/signatures/export.xlsx"


# --- /certificates/export.xlsx ---------------------------------------------


async def test_the_certificates_export_mirrors_the_list(
    client_a, client_b, bound_cert_of_a, user_a: User
):
    """The caller's own certificates and nobody else's, the owner resolved
    to a name, and the cap. No permission gate on `/certificates` at all —
    every authenticated caller gets their OWN (possibly empty) list, and
    `client_b`'s empty file is the list's own shape for "no rows"."""
    mine = await client_a.get("/api/v1/certificates")
    assert [c["id"] for c in mine.json()["items"]] == [str(bound_cert_of_a)]

    resp = await client_a.get(CERTIFICATES, params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Субъект" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == {str(bound_cert_of_a)}
    (row,) = rows
    assert row[5] == user_a.full_name  # the resolved user name, not a bare id

    theirs = await client_b.get(CERTIFICATES)
    assert theirs.status_code == 200
    assert xlsx_rows(theirs.content)[1] == []

    pkcs7 = _pkcs7(b"another-challenge-" + uuid.uuid4().bytes, user_a.pinfl)
    bound = await client_a.post("/api/v1/certificates", json={"pkcs7": pkcs7})
    assert bound.status_code == 201, bound.text
    with export_cap(1):  # two of A's certificates, one fits
        resp = await client_a.get(CERTIFICATES)
        assert resp.headers["x-export-total"] == "2"
        assert_export_cut(resp, cap=1)


# --- /signatures/export.xlsx -----------------------------------------------


async def test_the_signatures_export_mirrors_the_list(
    client_a, a_signature: Signature, user_a: User, db
):
    """One object's signatures as the list gives them, labels and the
    signer's name in the cells, and the cap."""
    listed = await client_a.get(
        f"/api/v1/signatures?object_type=permit&object_id={a_signature.object_id}"
    )
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert str(a_signature.id) in listed_ids

    params = {"object_type": "permit", "object_id": str(a_signature.object_id)}
    resp = await client_a.get(SIGNATURES, params={**params, "lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Тип объекта" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    _, rows = xlsx_rows(
        (await client_a.get(SIGNATURES, params={**params, "lang": "uz_latn"})).content
    )
    (row,) = rows
    assert row[3] == "ERI"  # kind LABEL, not "eri"
    assert row[4] == user_a.full_name  # the resolved signer name
    assert row[6] == "Haqiqiy"  # verification_status LABEL, not "valid"

    # A SECOND valid signature against the SAME object, under a different
    # purpose (`uq_signatures_valid_purpose` allows one valid signature per
    # purpose, not per object) -- so this object now carries 2 rows to cap.
    await service.sign(
        db,
        object_type="permit",
        object_id=a_signature.object_id,
        purpose="permit_accountant",
        document=DOC,
        pkcs7=_pkcs7(DOC, user_a.pinfl),
        user=user_a,
    )
    await db.commit()
    with export_cap(1):
        resp = await client_a.get(SIGNATURES, params=params)
        assert resp.headers["x-export-total"] == "2"
        assert_export_cut(resp, cap=1)


async def test_signatures_export_requires_ownership_or_view_any(client_b, a_signature: Signature):
    """The SAME status the list gives a non-owner without `view_any`
    (brief's shape 5)."""
    resp = await client_b.get(
        SIGNATURES, params={"object_type": "permit", "object_id": str(a_signature.object_id)}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"
