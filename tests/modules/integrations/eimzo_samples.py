"""Shared e-imzo-server response fixtures for the wire-parsing tests.

Stage 5.2 splits the vendor's JSON shapes across three tasks that must all
agree on what a real response looks like: Task 2 (`eimzo_wire.py`) parses
`/backend/pkcs7/verify/attached` and needs `VENDOR_ATTACHED_SAMPLE`; Task 3
adds the login flow and the detached-signature path and needs
`VENDOR_AUTH_SAMPLE`/`VENDOR_DETACHED_SAMPLE`. Defined once here, module-level,
so every task imports the same fixtures rather than each inventing its own —
a second, slightly-different "vendor sample" would defeat the point of
testing against the vendor's own shape at all.

Each constant's shape is checked against two sources: the official README at
github.com/qo0p/e-imzo-doc (response schemas and the truncated
`/backend/pkcs7/verify/attached` example) and the field names read out of the
unpacked e-imzo-server v2.1.1 jar's `uz/eimzo/server/json/` package
(`Pkcs7VerifyJsonResponse`, `Pkcs7InfoJson`, `Pkcs7SignerInfoJson`,
`CertificateJson`, `PublicKeyInfoJson`/`PublicKeyParameterInfoJson`,
`TimeStampInfoJson`, `SubjectCertificateInfoJson`, `AuthJsonResponse`) via
`javap`, which settles the couple of places the README's prose is ambiguous
(e.g. that `publicKey` on a pkcs7 certificate entry inherits `keyAlgName` and
`paramSetOID` from `PublicKeyParameterInfoJson`, not a nested object)."""

from typing import Any

# Verbatim vendor sample for POST /backend/auth, published 2026-05-25 with
# e-imzo-server v2.1.1. The inner `subjectCertificateInfo` object is exactly
# as published; the outer envelope (a sibling `status`/`message`) is the
# shape documented for this endpoint in the README and confirmed by
# `AuthJsonResponse` (extends `JsonResponse`, which carries `status`/
# `message`, plus its own `subjectCertificateInfo` field).
#
# Note this sample's own `subjectName` uses `"UID"`/`"CN"` keys rather than
# the OID-keyed form (`"1.2.860.3.16.1.2"`) the README's older example and
# `read_subject`'s OID constants use — both forms are real, and this sample
# is kept exactly as the vendor published it rather than "corrected" to
# match the OID form.
VENDOR_AUTH_SAMPLE: dict[str, Any] = {
    "subjectCertificateInfo": {
        "serialNumber": "218712ed3",
        "X500Name": "CN=XXX,UID=1234",
        "subjectName": {"UID": "1234", "CN": "XXX"},
        "validFrom": "2026-05-25 15:47:22",
        "validTo": "2026-06-24 15:47:22",
        "publicKeyParameter": {
            "keyAlgName": "OZMST-286-2024-2",
            "paramSetOID": "1.2.860.3.15.2.1.2.1.1",
        },
    },
    "status": 1,
    "message": "",
}

# A single signer, shared by the attached and detached samples below.
_SIGNER: dict[str, Any] = {
    "signerId": {
        "issuer": "CN=O'zDSt CA,O=e-imzo.uz",
        "subjectSerialNumber": "218712ed3",
    },
    "signingTime": "2026-05-25 15:48:10",
    "signature": "a88ab92b3eed2221925a8532a88ff52d",
    "digest": "3369cd520c8e556502b9bc0ac34ca69c",
    "verified": True,
    "certificateVerified": True,
    "certificateValidAtSigningTime": True,
    "policyIdentifiers": ["1.2.860.3.16.1.1"],
    "OCSPResponse": "MIIGdTCBrqADAgEAMIGkMIGh",
    "statusUpdatedAt": "2026-05-25 15:50:00",
    "statusNextUpdateAt": "2026-05-25 16:50:00",
    "certificate": [
        {
            # OID-keyed, per the README's `/backend/pkcs7/verify/attached`
            # example (`CertificateJson.subjectInfo`) — a different key than
            # `VENDOR_AUTH_SAMPLE`'s own `subjectName`, and deliberately so:
            # `read_subject` takes either shape, this fixture exercises the
            # OID form.
            "subjectInfo": {
                "1.2.860.3.16.1.2": "31234567890123",
                "CN": "ALIYEV ALI ALIYEVICH",
            },
            "issuerInfo": {"CN": "O'zDSt CA", "O": "e-imzo.uz"},
            "serialNumber": "218712ed3",
            "subjectName": "CN=ALIYEV ALI ALIYEVICH,UID=31234567890123",
            "validFrom": "2026-05-25 15:47:22",
            "validTo": "2026-06-24 15:47:22",
            "issuerName": "CN=O'zDSt CA,O=e-imzo.uz",
            # `PublicKeyInfoJson extends PublicKeyParameterInfoJson` (javap):
            # `keyAlgName`/`paramSetOID` are inherited fields, serialized
            # alongside `publicKey` in the same JSON object, not nested.
            # `paramSetOID` reuses the vendor's own `/backend/auth` sample
            # value — the same server build, same curve.
            "publicKey": {
                "keyAlgName": "OZMST-286-2024-2",
                "paramSetOID": "1.2.860.3.15.2.1.2.1.1",
                "publicKey": "MEgwEAYHKoZIzj0CAQYFK4EGAQQDNAA=",
            },
            "signature": {
                "signAlgName": "OZDST-286-2024-2",
                "signature": "a88ab92b3eed2221925a8532a88ff52d",
            },
        }
    ],
    "timeStampInfo": {
        "time": "2026-05-25 15:48:12",
        "tsa": "http://tsa.e-imzo.uz",
        "serialNumber": "9a11ffc2",
        "digestVerified": True,
        "certificateVerified": True,
        "verified": True,
    },
}

# `/backend/pkcs7/verify/attached`: `Pkcs7InfoJson` carries `documentBase64`
# alongside `signers` (javap) — the README's example shows the same pair.
VENDOR_ATTACHED_SAMPLE: dict[str, Any] = {
    "status": 1,
    "message": "",
    "pkcs7Info": {
        "documentBase64": "c29tZSBkb2N1bWVudA==",
        "signers": [_SIGNER],
    },
}

# `/backend/pkcs7/verify/detached`: identical response shape to `attached`
# except `pkcs7Info.documentBase64` is absent (README: the caller already
# holds the document, so the server has no reason to echo it back).
VENDOR_DETACHED_SAMPLE: dict[str, Any] = {
    "status": 1,
    "message": "",
    "pkcs7Info": {
        "signers": [_SIGNER],
    },
}
