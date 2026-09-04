"""Demo-sprint seed (Track 0, `docs/plans/06-demo-sprint.md`). Idempotent:
running it twice changes nothing on the second run and never raises.

Fills whatever the demo scenario needs that reference-data migrations do not
already provide:

  * one staff user per role the demo scenario touches, with a known password
    and a TOTP secret already enrolled (never `must_change_password`, so every
    route past `GET /auth/me` is reachable right after login+MFA);
  * one applicant with a finished registration (`applicants` row of their own,
    so `get_current_user` never raises ERR-AUTH-008);
  * the Burchmulla leshoz's real geodata, imported through `gis`'s own
    import pipeline (create -> parse -> submit-review -> approve -> publish),
    never a raw table insert — so the result is genuinely `GET /gis/contours`
    -visible, not a shortcut around the lifecycle;
  * the ten `coef_sb:*` rule parameters PUBLISHED for the demo, so grazing
    prices instead of refusing with ERR-NORM-004. They ship as DRAFTS on
    purpose (migration 0012, plan 03.7 ruling 8) pending VMQ 689's annex 5 —
    publishing them here is a demo-only convenience, loudly flagged, never a
    claim that the numbers are final.

Everything else the wizard needs (activity types, livestock types, VMQ 278
tariffs) is already seeded by migrations 0005/0012 — this script only checks
that it is there.

Usage: `uv run python -m app.seed.demo` (== `make demo-seed`). Targets
`DATABASE_URL` (the shared dev database, CLAUDE.md's "be deliberate about it"
— this is the one database the front-end tracks hit).
"""

import asyncio
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import files as core_files
from app.core import settings_store
from app.core.crypto import decrypt_str, encrypt_str
from app.core.schemas import PageParams
from app.core.security import (
    hash_password,
    hash_token,
    new_token,
    new_totp_secret,
    totp_provisioning_uri,
    validate_password_policy,
)
from app.db import make_engine, make_session_factory
from app.modules.admin import repo as admin_repo
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth import service as auth_service
from app.modules.auth.models import OtpCode, Role, User
from app.modules.gis import import_service as gis_import_service
from app.modules.gis import service as gis_service
from app.modules.norms.models import RuleParameter
from app.modules.norms.service import PARAMETER
from app.modules.norms.service import publish_versioned as norms_publish_versioned
from app.seed import seed_organizations

DEMO_PASSWORD = "Demo#Seed2026"  # meets tz/11's policy; printed, never secret
BURCHMULLA_CODE = "burchmulla"

# The agency root + the Burchmulla leshoz (decision #45), for a truly empty
# database — `app.seed.seed_organizations` is idempotent by `code`, so this is
# a no-op wherever an operator already seeded a real `organizations.json`
# (this dev database's own history: both rows already exist here).
_ORGANIZATION_ROWS = [
    {
        "code": "agency",
        "kind": "agency",
        "name": {"uz_cyrl": "Ўрмон хўжалиги агентлиги", "en": "Forestry Agency"},
    },
    {
        "code": BURCHMULLA_CODE,
        "kind": "leshoz",
        "parent_code": "agency",
        "name": {"uz_cyrl": "Бурчмулла ДЎХ", "en": "Burchmulla forestry"},
    },
]

# Source data (decision #45); read-only, never modified.
_SHAPEFILE_DIR = Path("/Users/oybek/projects/forest/ruxsatnoma/data/geodata/burchmulla/shapefile")
_SHAPEFILE_STEM = "Ижарачилар"
_SHAPEFILE_EXTS = (".shp", ".dbf", ".prj", ".shx", ".cpg")

# The importer's attribute map (import_service.NUMBER_KEY & co) for THIS file's
# DBF field names (README.md's table): the contour number, the declared area
# and the leshoz name the request itself already carries (organization_id).
_ATTRIBUTE_MAP = {
    "number": "c152_Exc_2",
    "declared_area_ha": "c152_Exc_5",
    "organization_name": "c152_Exc_1",
}

_MIN_PDF = b"%PDF-1.4\n%demo approval decree - not a real legal document\n%%EOF\n"


@dataclass(frozen=True)
class DemoUser:
    login: str
    full_name: str
    role_code: str
    organization_code: str | None = None
    pinfl: str | None = None


DEMO_STAFF: list[DemoUser] = [
    DemoUser("demo_sysadmin", "Demo Sys Admin", "sys_admin"),
    DemoUser("demo_executor", "Demo Executor (Burchmulla DOX)", "executor_staff", "burchmulla"),
    DemoUser(
        "demo_executor_head",
        "Demo Executor Head (Burchmulla DOX)",
        "executor_head",
        "burchmulla",
    ),
    # Holds `norms.tariffs.publish` for real (migration 0010's grant) — used to
    # publish the demo's coef_sb:* drafts so that gate is exercised on its own
    # merit, not on the sys_admin superuser bypass (`_holds_tariffs_publish`).
    DemoUser("demo_central_admin", "Demo Central Office Admin", "central_admin"),
    DemoUser("demo_accountant", "Demo Accountant", "accountant"),
    DemoUser("demo_prosecutor", "Demo Prosecutor", "prosecutor"),
]
DEMO_APPLICANT = DemoUser("demo_applicant", "Demo Applicant", "applicant", pinfl="20260904000001")
DEMO_APPLICANT_PHONE = "+998901112233"


async def _ensure_organizations(db: AsyncSession) -> tuple[int, int]:
    """The agency root and the Burchmulla leshoz — `app.seed.seed_organizations`
    is the module's own idempotent import path (match by `code`; a bad row
    would raise `ERR-VAL-001`, never write something `/refs` later chokes
    on), the same function `make seed KIND=organizations FILE=...` drives."""
    created, updated = await seed_organizations(db, _ORGANIZATION_ROWS)
    return created, updated


async def _ensure_user(
    db: AsyncSession, spec: DemoUser, *, shared_secret: str
) -> tuple[User, str, bool]:
    """Get-or-create one demo account. Returns (user, its TOTP secret, created?).

    The password is the fixed, printed `DEMO_PASSWORD` regardless of whether
    the row already existed — we chose it, we do not need to read it back.
    The TOTP secret DOES need to be read back on a rerun (it is genuinely
    random per first run), so an existing user's own `mfa_secret` is decrypted
    rather than re-generated — a fresh secret would silently invalidate an
    already-configured authenticator.
    """
    existing = (await db.execute(select(User).where(User.login == spec.login))).scalar_one_or_none()
    if existing is not None:
        secret = decrypt_str(existing.mfa_secret) if existing.mfa_secret else shared_secret
        return existing, secret, False

    role_id = (await db.execute(select(Role.id).where(Role.code == spec.role_code))).scalar_one()
    organization_id = None
    if spec.organization_code is not None:
        org = await admin_repo.get_organization_by_code(db, spec.organization_code)
        if org is None:
            raise RuntimeError(
                f"organization {spec.organization_code!r} not found — "
                "seed organizations before demo users"
            )
        organization_id = org.id

    user = User(
        login=spec.login,
        full_name=spec.full_name,
        role_id=role_id,
        organization_id=organization_id,
        pinfl=spec.pinfl,
        password_hash=hash_password(DEMO_PASSWORD),
        mfa_secret=encrypt_str(shared_secret),
        must_change_password=False,  # the one thing bootstrap.py's users start with (ruling 8)
    )
    db.add(user)
    await db.flush()
    await audit.log(
        db,
        action="user.create",
        object_type="user",
        object_id=user.id,
        basis="demo seed CLI",
        extra={"login": spec.login, "role": spec.role_code},
    )
    return user, shared_secret, True


async def _ensure_applicant_registration(db: AsyncSession, user: User) -> bool:
    """`registration_complete = true`: an `applicants` row owned by `user`.

    Drives the REAL `auth.service.complete_registration` rather than a raw
    insert (it also stamps `users.phone`/`phone_verified_at` and writes the
    consent rows a real registration leaves behind). The one thing skipped is
    the SMS round-trip: a `phone_verify_token` OtpCode row is planted directly
    in the shape `verify_otp` itself produces, exactly as `bootstrap.py`
    plants a password hash directly instead of driving a signup form.
    """
    if await auth_repo.get_own_applicant(db, user.id) is not None:
        return False
    privacy_version = await settings_store.get_str(db, "privacy_policy_version")
    offer_version = await settings_store.get_str(db, "offer_version")
    token = new_token()
    await auth_repo.add(
        db,
        OtpCode(
            target_type="phone",
            target=DEMO_APPLICANT_PHONE,
            code_hash=hash_token(token),
            purpose="phone_verify_token",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        ),
    )
    await auth_service.complete_registration(
        db,
        user,
        privacy_policy_version=privacy_version,
        offer_version=offer_version,
        phone=DEMO_APPLICANT_PHONE,
        otp_token=token,
        email=None,
        region_id=None,
        district_id=None,
        address="Toshkent shahri, demo address",
        ip=None,
    )
    return True


async def _publish_draft_coef_sb(db: AsyncSession, *, actor: User) -> list[str]:
    """Publish whichever `coef_sb:*` rows are still DRAFT, using the values
    already sitting in the database (migration 0012's provisional set) — never
    inventing a new number. `publish_versioned` is the module's own
    maker-checker publish path; these rows have `created_by IS NULL`
    (migration-seeded), so the maker-checker identity check is exempt, but the
    CHECKER permission gate still applies — `actor` must hold
    `norms.tariffs.publish` for real (`demo_central_admin`, migration 0011's
    grant to `central_admin`), not pass on the `sys_admin` superuser bypass,
    so this exercises the same gate a real publish would (decision #62).

    Returns the codes actually published (empty when they already were, or
    when there is nothing to do)."""
    rows = (
        (
            await db.execute(
                select(RuleParameter).where(
                    RuleParameter.code.like("coef_sb:%"), RuleParameter.status == "draft"
                )
            )
        )
        .scalars()
        .all()
    )
    published: list[str] = []
    for row in rows:
        await norms_publish_versioned(db, PARAMETER, row.id, actor=actor)
        published.append(row.code)
    return published


def _zip_shapefile() -> bytes | None:
    """Blocking filesystem work (`Path.exists`/`.write`) — called through
    `asyncio.to_thread`, never on the event loop, the same discipline
    `gis.importer.parse` documents for GDAL. `None` means the source
    directory itself is missing (data/ is outside git — decision #45)."""
    if not _SHAPEFILE_DIR.exists():
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for ext in _SHAPEFILE_EXTS:
            path = _SHAPEFILE_DIR / f"{_SHAPEFILE_STEM}{ext}"
            if path.exists():
                zf.write(path, arcname=path.name)
    return buf.getvalue()


async def _ensure_burchmulla_contours(db: AsyncSession, *, actor: User) -> str:
    """Import Burchmulla's 151 real polygons through `gis`'s own pipeline
    (`import_service.create_import`/`run_import`, then
    `gis_service.submit_import_review`/`approve_import`/`publish_import`) —
    never a raw `contours`/`contour_versions` insert, so the result is
    genuinely a PUBLISHED version each, visible to `GET /gis/contours` the
    same way a hand-drawn one would be.

    Idempotent by CONTENT, not by a marker row: if Burchmulla already has any
    published contour, importing again would only add duplicate-numbered
    siblings (`import_service._unique_number`'s `/2` suffix), so this checks
    first and does nothing when the leshoz is already on the map.

    `actor` runs the whole pipeline (create -> submit-review -> approve ->
    publish): the zone check every step applies (`gis.service._assert_in_zone`)
    is a no-op for a zone empty on all three axes, which is what makes a
    single sys_admin actor a legitimate stand-in for both the GIS specialist
    who normally imports and the executor_head who normally approves — this
    call never goes through the router's `require_permission`, which is the
    ONLY place that distinction is enforced (CLAUDE.md's GIS section)."""
    org = await admin_repo.get_organization_by_code(db, BURCHMULLA_CODE)
    if org is None:
        return f"SKIPPED: organization {BURCHMULLA_CODE!r} not found — seed organizations first"

    _items, total = await gis_service.list_contours(
        db,
        organization_id=org.id,
        bbox=None,
        params=PageParams(page=1, page_size=1),
        actor=actor,
    )
    if total > 0:
        return f"Burchmulla already has {total} published contour(s) — import skipped"

    data = await asyncio.to_thread(_zip_shapefile)
    if data is None:
        return f"WARNING: shapefile source missing at {_SHAPEFILE_DIR} — cannot import Burchmulla"

    approval_doc = await core_files.save_upload(
        db,
        data=_MIN_PDF,
        filename="demo-burchmulla-approval.pdf",
        content_type="application/pdf",
        actor=actor,
    )
    row = await gis_import_service.create_import(
        db,
        layer_code="contours",
        organization_id=org.id,
        approval_doc_id=approval_doc.id,
        fmt="shp",
        attribute_map=_ATTRIBUTE_MAP,
        data=data,
        filename="burchmulla-ijarachilar.zip",
        content_type="application/zip",
        actor=actor,
    )
    await gis_import_service.run_import(db, row)
    if row.status == "failed":
        return f"FAILED to import Burchmulla geodata: {row.error_report}"

    await gis_service.submit_import_review(db, row.id, actor=actor)
    await gis_service.approve_import(db, row.id, actor=actor)
    result = await gis_service.publish_import(db, row.id, actor=actor)
    published = result["published"]
    blocked = result["blocked"]
    msg = f"Imported Burchmulla: {published} published"
    if blocked:
        msg += f", {len(blocked)} blocked (see gis_imports.stats for the check report)"
    return msg


async def _main() -> None:
    validate_password_policy(DEMO_PASSWORD)
    engine = make_engine(get_settings().database_url)
    factory = make_session_factory(engine)
    report: list[str] = []
    try:
        # --- Organizations (agency root + Burchmulla leshoz) ----------------
        async with factory() as db:
            org_created, org_updated = await _ensure_organizations(db)
            await db.commit()
        report.append(
            f"Organizations: {org_created} created, {org_updated} updated (agency, burchmulla)"
        )

        # --- Users -------------------------------------------------------
        shared_secret = new_totp_secret()
        created_users: dict[str, tuple[User, str, bool]] = {}
        async with factory() as db:
            for spec in [*DEMO_STAFF, DEMO_APPLICANT]:
                user, secret, created = await _ensure_user(db, spec, shared_secret=shared_secret)
                created_users[spec.login] = (user, secret, created)
            await db.commit()

        report.append("=== Demo accounts (password + TOTP for every one) ===")
        report.append(f"password (all accounts): {DEMO_PASSWORD}")
        for spec in [*DEMO_STAFF, DEMO_APPLICANT]:
            user, secret, created = created_users[spec.login]
            report.append(
                f"  {spec.login:20s} role={spec.role_code:15s} "
                f"{'(created)' if created else '(already existed)'}"
            )
            report.append(f"      TOTP secret: {secret}")
            report.append(f"      TOTP URI:    {totp_provisioning_uri(secret, spec.login)}")

        # --- Applicant registration ---------------------------------------
        async with factory() as db:
            applicant_user = (
                await db.execute(select(User).where(User.login == DEMO_APPLICANT.login))
            ).scalar_one()
            applicant_created = await _ensure_applicant_registration(db, applicant_user)
            await db.commit()
        report.append("")
        report.append(
            "Applicant registration: "
            + ("completed now" if applicant_created else "already complete")
        )

        # --- coef_sb (grazing) demo publish ---------------------------------
        async with factory() as db:
            central_admin_user = (
                await db.execute(select(User).where(User.login == "demo_central_admin"))
            ).scalar_one()
            published_codes = await _publish_draft_coef_sb(db, actor=central_admin_user)
            await db.commit()
        report.append("")
        if published_codes:
            report.append("*" * 78)
            report.append("*** WARNING — DEMO DATABASE ONLY (decision #62, Oybek 2026-09-04): ***")
            report.append(
                "*** Published the ten coef_sb:* conditional-head coefficients so     ***"
            )
            report.append(
                "*** grazing prices instead of refusing (ERR-NORM-004). These values  ***"
            )
            report.append(
                "*** are PROVISIONAL (migration 0012) and are NOT legally final — VMQ  ***"
            )
            report.append(
                "*** 689's annex 5 has not arrived from the Agency. Never publish this ***"
            )
            report.append("*** in a migration or in any non-demo environment.                ***")
            report.append("*" * 78)
            report.append(f"Published: {', '.join(published_codes)}")
        else:
            report.append(
                "coef_sb:* rule parameters: already published (or none were draft) — "
                "still PROVISIONAL/not legally final if they were published by this "
                "command (decision #62)"
            )

        # --- Burchmulla geodata ---------------------------------------------
        async with factory() as db:
            sysadmin_user = (
                await db.execute(select(User).where(User.login == "demo_sysadmin"))
            ).scalar_one()
            gis_message = await _ensure_burchmulla_contours(db, actor=sysadmin_user)
            await db.commit()
        report.append("")
        report.append(gis_message)
    finally:
        await engine.dispose()

    print("\n".join(report))


if __name__ == "__main__":
    asyncio.run(_main())
