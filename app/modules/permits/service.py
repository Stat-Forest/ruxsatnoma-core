"""Permits service — issuance, and the in-process reads a permit's document needs.

Issuance is the point of the whole system: a PAID application becomes a numbered,
rendered, hash-frozen document. Three invariants govern this file.

**The snapshot is immutable** (`tz/05` invariant 7, ruling 14). Every field the PDF
shows is copied into `permits.snapshot` at issuance and read from there forever
after. An applicant who renames themselves tomorrow does not change a permit issued
today, and a permit re-rendered from its own snapshot years later — after the tariff
has changed twice — is the same document.

**The document is rendered once and its hash frozen** (ruling 3). `doc_hash` is
`sha256` of the stored bytes, and all four ERI signatures are taken over exactly
those bytes, which is what makes `signatures.service.require_complete` mean what it
says. A corrected permit is a revocation plus a new permit, never a re-render.

**`permits` may not read `payments`** (`design/01` rule 3 — both are level 4 and
neither may call the other). The application reaching `PAID` is 3.10a's job and is
the only fact this module needs; the check here is on the application's own status
and never on an invoice.
"""

import asyncio
import hashlib
import secrets
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import files, storage
from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import err
from app.core.models import MediaFile
from app.core.schemas import PageParams
from app.core.time import business_today
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import Organization
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth import service as auth_service
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import Applicant, User
from app.modules.gis import service as gis_service
from app.modules.notifications import service as notifications
from app.modules.permits import events, render, repo, signers
from app.modules.permits.models import (
    Permit,
    PermitStatusHistory,
    PermitTemplate,
    QrCheckLog,
)
from app.modules.permits.permissions import PERMITS_VIEW_ANY
from app.modules.signatures import service as signatures_service

# Audit action codes: "<object>.<verb>" in English, and the constant lives with the
# acting module — audit is level 0 and knows no domain vocabulary (decision #38,
# ruling 17). The dot is shared with `notification_templates.event_code`
# (`permit.issued`) and neither is a bus name; see `permits/events.py`.
PERMIT_ISSUE = "permit.issue"
PERMIT_SIGN = "permit.sign"
# What `set_status` writes — the generic move, named for what it is rather than
# for any one caller, because 3.11b's suspend/resume/revoke and 4.7's archival
# all come through it. A flow verb that does MORE than move a status audits
# under its own name instead (`PERMIT_SIGN`, `PERMIT_EXPIRE`), the same split
# `applications.service` makes between `APPLICATION_STATUS_CHANGE` and its flow
# verbs.
PERMIT_STATUS_CHANGE = "permit.status_change"
# Written ONLY on a denial, never on a successful read (ruling T8-a). A GET that
# audits every hit lets anyone holding a session write an `audit_log` row per
# request; a GET that audits nothing leaves `tz/10`'s RI-12 — «попытка доступа
# вне территориальных полномочий», High and immediate — with no way to fire on a
# READ at all, while the same actor's attempt to SIGN outside their zone is
# already recorded (`_signer_refusal`'s `wrong_organization`). See
# `_readable_permit`.
PERMIT_READ = "permit.read"
# The two the daily sweeps write (`permits/jobs.py`). `permit.close_application`
# is named for what it does rather than shortened to `permit.close`: the permit
# is not closed and never will be — `expired`/`revoked` are terminal until 4.7
# archives them — it is the APPLICATION that reaches CLOSED (ruling 13).
PERMIT_EXPIRE = "permit.expire"
PERMIT_CLOSE_APPLICATION = "permit.close_application"

# The permit's initial status, and the one it reaches when the last required
# signature lands. Nothing else in this codebase may write `active` onto a permit:
# see `_activate`, which is reachable only from `add_signature` and only once
# `signatures.service.missing_purposes` has come back empty (C11).
INITIAL_STATUS = "pending_signatures"
ACTIVE_STATUS = "active"

# `tz/05`'s permit state machine, verbatim, plus the one row `tz/05` does not
# have: `pending_signatures`, which this project added because C11 makes a permit
# legally real only once all 3+1 signatures are on it. Every one of
# `models.PERMIT_STATUSES` is a key, `archived` is terminal, and no status maps
# to ITSELF — `tz/05` has no self-loop anywhere, so a repeat move to the status a
# permit already holds is exactly as illegal as any other jump, which is how a
# retrying caller tells "already applied, harmless" (`details["from"] ==
# details["to"]`) from a genuine mistake. Same discipline, same shape, as
# `applications.service.APPLICATION_TRANSITIONS`.
#
# `pending_signatures` has ONE exit and it is `active`. That is deliberate and it
# is a gap somebody will meet: a permit formed by mistake, or one whose recipient
# simply never signs, cannot be revoked or expired — the expiry sweep skips it on
# purpose (a permit that never came into force cannot be «муддати тугаган») and
# nothing times out a citizen's signature. The plan already records the question
# for the Agency; whoever answers it adds the edge here WITH the transition that
# writes it, rather than discovering the table refuses them.
PERMIT_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending_signatures": frozenset({"active"}),
    "active": frozenset({"suspended", "revoked", "expired"}),
    "suspended": frozenset({"active", "revoked", "expired"}),
    "revoked": frozenset({"archived"}),
    "expired": frozenset({"archived"}),
    "archived": frozenset(),
}

# What `applications` calls the state a signed permit puts it in. `tz/05` defines
# it as «сформировано **и подписано**», which is why issuance does NOT set it
# (ruling 18) — the last signature does.
APPLICATION_PERMIT_ISSUED = "PERMIT_ISSUED"

# `tz/13` field 19 is «Статус оплаты и дата». The STATUS is what this module can
# state on its own authority — issuance runs from `PAID` and from nothing else, so
# the word is a constant rather than a lookup. The DATE half is not stored here and
# is not on the document: it lives in `payments`, which this module may not read
# (design/01 rule 3), and `applications.service` exposes no paid-at. Printing the
# issuance date in its place would put a wrong date on a legal document.
#
# Ruling T3-b, and the part that needs saying out loud: because the snapshot is
# immutable and is never re-derived, every permit issued BEFORE a lawful source for
# that date exists carries «Тўланган» with no date PERMANENTLY. Adding the accessor
# later fixes the permits issued after it, and none of the ones issued before.
PAYMENT_STATUS_PAID = "Тўланган"

# The document's language. `tz/13`'s note: «на государственном языке» — the permit
# is issued in Uzbek Cyrillic, whatever language the holder reads the cabinet in.
# `organizations.name`/`activity_types.name` are JSONB with this key.
DOCUMENT_LANGUAGE = "uz_cyrl"

# What a field prints when the form has nothing to state there — an em dash, the form
# convention. It is a chosen VALUE, not a missing one, and it is never a blank.
#
# Two requisites use it, for two different reasons. A head-count row (12-15): never
# "0", because `CalcRequest.items` is empty for every activity but grazing (`quantity`
# carries those) and an apiary permit reading «Қорамол — 0» would be a statement about
# cattle that nobody made. The holder's address (11, ruling T3-g): the registry
# genuinely may not hold one, and "not on file" is the truthful thing to print.
NOT_STATED = "—"

# `tz/13` requisites 12-15: form 1-ilova's four head-count rows, and which
# `livestock_types.code` (migration 0005) belongs to each.
#
#   12 «Скот, взрослые: КРС, лошади, верблюды, ослы»
#   13 «Скот, молодняк до 2 лет: КРС, лошади, верблюды, ослы»
#   14 «Старше 6 мес: овцы, козы»
#   15 «До 6 мес: ягнята, козлята»
#
# Written down here rather than derived from `rule_parameters`' `tariff_group:<code>`,
# which draws the identical partition today: that one answers which VMQ 278 RATE a
# species is charged at, and this one answers which line of the FORM it is printed on.
# They agree by law, not by construction, and a tariff regrouping must not silently
# redraw a state document. A code in neither row is refused, never dropped — see
# `_livestock_rows`.
LIVESTOCK_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("heads_large_adult", ("cattle_adult", "horse_adult", "camel_adult", "donkey_adult")),
    ("heads_large_young", ("cattle_young", "horse_young", "camel_young", "donkey_young")),
    ("heads_small_adult", ("sheep_goat_6m",)),
    ("heads_small_young", ("lamb_kid_under_6m",)),
)


def _organization_in_zone(zone: Zone, org: Organization) -> bool:
    """Per-row equivalent of `zone_filter`'s SQL for ONE organization row — a LOCAL
    copy of the private helper of the same name and identical logic in
    `gis.service` and `norms.service`. The module boundary (cross-module calls go
    through the other module's service) rules out importing either: it is not part
    of their declared public surface."""
    if zone.region_id is not None and zone.region_id != org.region_id:
        return False
    if zone.district_id is not None and zone.district_id != org.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != org.id:
        return False
    return True


async def _organization_in_actor_zone(
    db: AsyncSession, actor: User, organization_id: uuid.UUID
) -> bool:
    """Whether this actor's zone covers this organization — the module's ONE
    territorial rule, shared by the issuance path and by every read route (Task
    8), so the two can never drift into answering differently about the same
    leshoz.

    A predicate rather than only an assertion, because the read path has to WRITE
    a trail before it refuses (`_readable_permit`, ruling T8-a) and catching the
    assertion's own exception to do that would make the refusal's cause a thing
    inferred from an exception type rather than decided here.

    ALL THREE axes of `app/core/abac.py`'s `Zone`, not `organization_id` alone:
    narrowing it to one was a finding of 3.6a's own final review — an actor with a
    region but no organization of their own then passes for every organization in
    the country, and that shape is creatable today
    (`admin.users_service.create_user` sets the three columns independently).

    Separate from any permission check, which answers whether this role may act
    AT ALL (lesson: zone scoping is not a permission check).
    """
    zone = zone_of(actor)
    if zone == Zone(None, None, None):
        return True
    org = await admin_repo.get_organization(db, organization_id)
    return org is not None and _organization_in_zone(zone, org)


async def _assert_organization_in_zone(
    db: AsyncSession, actor: User, organization_id: uuid.UUID
) -> None:
    """`_organization_in_actor_zone` as the write paths use it — refuse, with no
    trail of its own. The READ path audits before it raises; issuance does not,
    and that asymmetry is deliberate: an unauthorized ISSUANCE never gets past
    this point silently, because `issue` audits the whole attempt either way."""
    if not await _organization_in_actor_zone(db, actor, organization_id):
        raise err("ERR-ACL-002")


async def _assert_in_zone(db: AsyncSession, actor: User, contour_id: uuid.UUID) -> uuid.UUID:
    """Which leshoz issues this permit, refusing an actor whose zone does not
    cover it. Returns that organization id, so the caller never resolves it twice.

    The organization is resolved through `gis.service` — never `gis.repo`, never a
    direct query of `contours` — exactly as `norms.service._assert_norm_zone` does;
    the zone comparison itself is `_assert_organization_in_zone` above.
    """
    organization_id = await gis_service.contour_organization(db, contour_id)
    if organization_id is None:
        raise err("ERR-SYS-003", details={"contour": str(contour_id)})
    await _assert_organization_in_zone(db, actor, organization_id)
    return organization_id


def _required[T](value: T | None, *, field: str) -> T:
    """A permit is a legal document: an unfilled requisite is a defect that must
    fail HERE, loudly and by name, rather than reach a citizen as a blank line or
    the word "None" (the renderer refuses it too — this says WHICH source was
    empty, which the renderer cannot know).

    Generic rather than `Any` so it also NARROWS: an `Application` is autosaved
    field by field and half of its columns are nullable (3.9a ruling 7), so every
    one of them reaches here as `T | None` and pyright checks that nothing skips
    this call on the way into the permit row."""
    if value is None or value == "":
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": field})
    return value


def _localized(name: Any, *, field: str) -> str:
    """A JSONB `{uz_cyrl: ..., ru: ...}` reference name, in the document's own
    language. No fallback to `ru`: a Russian leshoz name on an Uzbek-language
    permit is a defect that should be fixed in the classifier, not papered over."""
    if not isinstance(name, dict):
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": field})
    return str(_required(name.get(DOCUMENT_LANGUAGE), field=field))


def _money(value: Decimal | None) -> str | None:
    """A fixed-scale NUMERIC as the document prints it. `format(value, "f")` and
    never `str(Decimal)`, which can produce scientific notation."""
    return None if value is None else format(value, "f")


def _frozen_herd(input_snapshot: Any) -> dict[str, int]:
    """The head counts the permit is PRICED for, out of the calculation's own
    `input_snapshot` (ruling T3-f) — `{livestock_code: heads}`, summed per code.

    NOT a query on `application_items`. That table is live: 3.9b's recalculation
    path may edit it after the money was fixed, and a permit whose printed herd and
    printed amount came from different moments would be exactly the disagreement an
    immutable snapshot exists to rule out. `input_snapshot["request"]["items"]` is
    the herd `norms.calculator.calculate` actually charged for, frozen in the same
    row as `amount` — `calculator.from_input_snapshot` reads the identical path.

    An `input_snapshot` this cannot read is a refusal, not an empty herd: every
    calculation the calculator writes carries `request.items` (empty for a
    non-grazing activity, where `quantity` carries the amount instead), so a missing
    path means a row written by something else — and a permit must not print a
    silently empty herd on that evidence.
    """
    request = input_snapshot.get("request") if isinstance(input_snapshot, dict) else None
    items = request.get("items") if isinstance(request, dict) else None
    if not isinstance(items, list):
        raise err("ERR-VAL-001", details={"reason": "calculation_snapshot_unreadable"})
    herd: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict) or "livestock_code" not in item or "count" not in item:
            raise err("ERR-VAL-001", details={"reason": "calculation_snapshot_unreadable"})
        herd[str(item["livestock_code"])] = herd.get(str(item["livestock_code"]), 0) + int(
            item["count"]
        )
    return herd


async def _livestock_rows(db: AsyncSession, input_snapshot: Any) -> dict[str, str]:
    """`tz/13` requisites 12-15, ready to print: one string per row of form 1-ilova,
    naming each species from the classifier and its head count.

    Species order follows `LIVESTOCK_ROWS`, never the order the applicant happened to
    enter them in — two permits for the same herd must render the same bytes, which is
    what `doc_hash` and every signature over it depend on (ruling 3).

    A code belonging to no row is a REFUSAL. `livestock_types` is an admin catalogue
    and an eleventh species can be added without anyone touching this module; dropping
    it here would understate the herd on a legal permit while the fee — computed from
    the very same list — still charged for it.
    """
    herd = _frozen_herd(input_snapshot)
    known = {code for _, codes in LIVESTOCK_ROWS for code in codes}
    for code in herd:
        if code not in known:
            raise err("ERR-VAL-001", details={"reason": "unknown_livestock_code", "code": code})
    types = await admin_repo.get_livestock_types_by_code(db, herd)
    rows: dict[str, str] = {}
    for field, codes in LIVESTOCK_ROWS:
        printed = []
        for code in codes:
            if herd.get(code):
                livestock = types.get(code)
                if livestock is None:
                    raise err(
                        "ERR-VAL-001",
                        details={"reason": "unknown_livestock_code", "code": code},
                    )
                name = _localized(livestock.name, field=field)
                printed.append(f"{name} — {herd[code]}")
        rows[field] = ", ".join(printed) if printed else NOT_STATED
    return rows


def _reference_name(name: Any) -> str | None:
    """A JSONB reference name in the document's language, or None — the non-raising
    twin of `_localized`, for the one place where a missing name must degrade rather
    than refuse (the address parts of ruling T3-g). Everywhere else a classifier gap
    IS a defect and `_localized` says so."""
    if not isinstance(name, dict):
        return None
    value = name.get(DOCUMENT_LANGUAGE)
    return None if value is None or value == "" else str(value)


async def _holder_address(db: AsyncSession, applicant: Applicant) -> str:
    """`tz/13` requisite 11, composed from whatever the registry actually holds:
    region, district and free-text address, widest first, joined by commas.

    **Ruling T3-g: this never refuses an issuance.** `applicants.address` is nullable
    by 3.2b's design — `CompleteRegistrationIn.address` is `str | None`, the citizen
    supplies it and many will not — and blocking here would strand someone who has
    ALREADY PAID behind a profile edit only they can make, over a field that does not
    identify them. Requisite 10 (name plus PINFL/STIR) is the identity, and those
    columns really are non-null; this one is descriptive.

    That is not a softening of "an unfilled field is a defect", which is about a
    placeholder the LAYOUT declares and the snapshot fails to fill — a programming
    error the renderer must never paper over. A nullable source column recorded as
    `NOT_STATED` is a deliberate value; the em dash is chosen, not missing.

    If the Agency wants the address mandatory, the place for it is registration
    (3.2b), where the rule reaches every future applicant — refusing at issuance
    would only reach the ones who already paid.
    """
    parts: list[str] = []
    if applicant.region_id is not None:
        region = await admin_repo.get_region(db, applicant.region_id)
        if region is not None:
            parts.append(_reference_name(region.name) or "")
    if applicant.district_id is not None:
        district = await admin_repo.get_district(db, applicant.district_id)
        if district is not None:
            parts.append(_reference_name(district.name) or "")
    if applicant.address:
        parts.append(applicant.address)
    filled = [part for part in parts if part]
    return ", ".join(filled) if filled else NOT_STATED


# The FRONT-END path the printed QR points at, not the JSON API route behind it.
# Requisite 24's whole purpose is that a citizen scanning a printed permit lands on
# something readable; `/api/v1/public/permits/check` answers `{"found": true, …}`,
# which is the page's data source and not the page. Ruling 15 fixes where the ROUTE
# lives (`design/03`'s own path, implemented in this module rather than in the
# unbuilt module `public`) and says nothing about what the QR ENCODES.
#
# **Stage 6 must serve this path before the first production permit is issued** —
# until it does the URL 404s, which costs nothing today because no production permit
# exists, and becomes uncorrectable the day one does (`README.md`, deploy notes).
QR_CHECK_PATH = "/check"


def qr_url(token: str) -> str:
    """The address the printed QR points at. Built from `settings.public_base_url`,
    which must be the externally reachable origin — a permit is printed once and the
    URL on it cannot be corrected afterwards.

    `{public_base_url}/check?qr=…`, the page a human reads, NOT
    `/api/v1/public/permits/check`, which answers JSON to that page (see
    `QR_CHECK_PATH` above).

    Kept short on purpose: the QR's module size shrinks as the payload grows, and
    the bundled layout prints the symbol at 28 mm (task 2's inherited caveat)."""
    return f"{get_settings().public_base_url.rstrip('/')}{QR_CHECK_PATH}?qr={token}"


async def _layout_html(db: AsyncSession, template: PermitTemplate) -> str:
    """The layout this template means.

    `layout_file_id` NULL means "the layout bundled with the module"
    (`app/modules/permits/assets/default_layout.html`, task 1 decision 2): a
    migration cannot put bytes in MinIO, and a row pointing at a storage key that
    does not exist would be worse than an honest null. A non-null value is an
    administrator's own uploaded layout and wins from then on.
    """
    if template.layout_file_id is None:
        return render.default_layout()
    file = await db.get(MediaFile, template.layout_file_id)
    if file is None or file.status != "active":
        raise err(
            "ERR-VAL-001",
            details={"reason": "layout_file_missing", "file_id": str(template.layout_file_id)},
        )
    data = await storage.get_object(file.storage_key)
    return data.decode("utf-8")


async def _snapshot(
    db: AsyncSession,
    *,
    series: str,
    number: int,
    applicant_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    organization_id: uuid.UUID,
    contour_id: uuid.UUID,
    area_ha: Decimal,
    period_from: date,
    period_to: date,
    amount: Decimal,
    sb_load: Decimal | None,
    calculation_id: uuid.UUID,
    calculation_input: Any,
) -> dict[str, Any]:
    """Form 1-ilova's requisites (`tz/13` § 1-илова, ruling 14), gathered once and
    never read from their sources again.

    Everything is already a string: the snapshot is what the renderer receives, so
    the PDF and the stored record cannot disagree, and JSONB has no `Decimal` or
    `date` (lesson: nothing in this app configures a JSON encoder — coerce at the
    boundary).

    **Which of `tz/13`'s 25 requisites are here, and why the rest are not.** The
    table has three kinds of gap and they must not be confused, because a reader
    comparing this against the form will otherwise "fix" the wrong one:

    - **1-4, 8-19 are here.** Requisites 1 and 4 are two DIFFERENT organizations —
      the authorising agency and the leshoz whose ground the contour is on; the
      chain `agency → territorial → leshoz → …` is `organizations.kind`, with a
      single-agency partial unique index, and `contours.organization_id` is the
      leshoz. Requisites 12-15 come from the frozen calculation, not from
      `application_items` (ruling T3-f — see `_livestock_rows`). Requisite 11 is
      composed from the registry and prints `NOT_STATED` when it holds nothing,
      never refusing an issuance (ruling T3-g — see `_holder_address`).
      Requisite 19 ships
      as the payment STATUS with no date (ruling T3-b): there is no lawful source
      for the date, so a permit issued before one exists carries «Тўланган» with no
      date PERMANENTLY — the snapshot is immutable and is never re-derived.
    - **5-7 (ўрмон бўлими / айланма / бўлак) have no data source.** `organizations`
      can EXPRESS them — `kind` runs down to `bolim`, `aylanma`, `bolak` — but
      nothing populates those rows and every contour hangs off its leshoz, because
      the Agency has delivered neither the leshoz boundary nor the contour layer
      (`tz/12` open question #10). They arrive as data, not as code.
    - **20-23 (the four ERI signature lines) are structurally absent BY DESIGN,
      and this is not a gap to close.** Ruling 3 renders the document exactly once,
      at issuance, before anybody has signed, and refuses any re-render while a
      signature exists — that is what makes `sha256` of these bytes a stable thing
      to sign. The four signatures are detached rows in `signatures`; the way a
      reader confirms them is the QR page (requisite 24), which reads the stored
      verification verdict. Printing signer names would require re-rendering after
      signing, which would invalidate every signature already taken.
    - **24 and 25 are deliberately absent too.** №24 «печать подлинности» IS the QR,
      which the renderer fills itself; №25 «статус документа» is `permits.status`,
      which changes over the permit's life and must not be frozen into an immutable
      snapshot.

    `calculation_id` is not printed. It is here so the permit can be compared
    against the invoice that was actually paid (ruling 19): 3.9b's `recalculate`
    writes a new calculation row and "the newest wins", so without the id a permit
    could carry a figure the citizen never paid and nothing on this side would show
    it. A guard on another branch is not evidence.

    Every value arrives already validated and non-null: half of `applications`'
    columns are nullable because a DRAFT is autosaved field by field (3.9a ruling
    7), and `issue` resolves each through `_required` before calling this — so a
    missing requisite is named at its SOURCE rather than reaching the renderer as
    an unfilled placeholder it cannot attribute.
    """
    applicant = await auth_service.get_applicant(db, applicant_id)
    if applicant is None:
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": "applicant"})
    organization = await admin_repo.get_organization(db, organization_id)
    if organization is None:
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": "organization"})
    activity = await admin_repo.get_activity_type(db, activity_type_id)
    if activity is None:
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": "activity_type"})
    contour_number = await gis_service.contour_number(db, contour_id)
    # Requisite 1. The single root of the organization tree — `root_is_agency`
    # makes `parent_id IS NULL` and `kind = 'agency'` the same row, so an agency
    # necessarily exists wherever a contour has an organization at all.
    agency = await admin_repo.get_agency(db)
    if agency is None:
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": "authority"})
    heads = await _livestock_rows(db, calculation_input)

    return {
        # 1-2: the authorising body, the series and the number
        "authority_name": _localized(agency.name, field="authority_name"),
        "series": series,
        # Six digits — `design/03`'s «серия А № 000123», and the reason the API
        # returns the integer while the document shows the padded form.
        "number": f"{number:06d}",
        # 3: «Дата выдачи» — the calendar date the DOCUMENT bears, in Tashkent
        # (`business_today`, never `date.today()`: on a UTC container the server's
        # own date is yesterday for ~5 hours a day — lesson). Distinct from
        # `permits.issued_at`, the timestamp Task 4 sets when the last signature
        # makes the permit legally in force.
        "issued_at": business_today().isoformat(),
        # 4: «Ўрмон хўжалиги» — the leshoz the contour belongs to, and NOT the
        # authority above. `contours.organization_id` is that leshoz: the GIS
        # import and the operator seed both attach a contour to one, and no row
        # below leshoz level exists to attach it to (requisites 5-7, docstring).
        "leshoz_name": _localized(organization.name, field="leshoz_name"),
        # 8-11: the plot, the holder
        "activity_name": _localized(activity.name, field="activity_name"),
        "holder_name": str(_required(applicant.name, field="holder_name")),
        # An individual is identified by PINFL, a legal entity by STIR —
        # `identity_by_kind` (migration 0003) guarantees exactly one is set.
        "holder_pinfl": str(_required(applicant.pinfl or applicant.stir, field="holder_pinfl")),
        # 11: «Адрес пользователя» — composed from the registry, and «—» when it
        # holds nothing. Never a refusal (ruling T3-g); see `_holder_address`.
        "holder_address": await _holder_address(db, applicant),
        "contour_number": str(_required(contour_number, field="contour_number")),
        "area_ha": _money(area_ha),
        # 12-15: the herd, per form row, out of the frozen calculation (ruling T3-f)
        **heads,
        # 16-19: the load, the term, the money, the payment
        "sb_load": _money(sb_load),
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "amount": _money(amount),
        "payment_status": PAYMENT_STATUS_PAID,
        # Not printed: the link back to the calculation this amount came from.
        "calculation_id": str(calculation_id),
    }


async def issue(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> Permit:
    """Form the permit for a PAID application, in one transaction.

    The order below is load-bearing. In particular the counter is touched only
    after every input has been gathered: a series number handed out to a failed
    issuance is a gap in a legal register that nobody can explain years later, and
    `permit_counters` has no way to give one back.

    The application does NOT move to `PERMIT_ISSUED` here (ruling 18). `tz/05`
    defines that status as «сформировано **и подписано**», and four different
    people have yet to sign; Task 4 moves it when the last signature lands. Between
    paying and that moment an applicant's cabinet honestly shows «оплачено» with a
    permit attached and awaiting signatures.
    """
    application = await applications_service.get(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})

    # 1. PAID, and nothing else (ruling 10, tz/04 С11). A refused attempt is
    # RI-10, CRITICAL and immediate in tz/10 — written to the audit journal and
    # COMMITTED before the exception that explains it, because a raise rolls the
    # trail back with it (the early-commit-on-denial pattern, decision #40).
    if application.status != "PAID":
        await audit.log(
            db,
            action=PERMIT_ISSUE,
            user_id=actor.id,
            object_type="application",
            object_id=application.id,
            result="denied",
            basis="application is not PAID",
            new_value={"status": application.status},
            extra={"risk_indicator": "RI-10"},
        )
        await db.commit()
        raise err("ERR-PAY-001", details={"status": application.status})

    # 2. One permit per application (`permits.application_id` is unique). Caught
    # here as a domain answer rather than left to the constraint, which would
    # surface as an IntegrityError 500 the applicant reads as "issuance failed".
    # This is also why the route needs no `Idempotency-Key`: a replayed request is
    # refused with ERR-PERM-001, never answered with a second document.
    if await repo.permit_by_application(db, application_id) is not None:
        raise err("ERR-PERM-001", details={"reason": "already_issued"})

    # 3. Everything the document says, gathered once. A DRAFT is autosaved field
    # by field, so most of these columns are nullable (3.9a ruling 7) and each is
    # resolved through `_required` HERE — a missing requisite is named at its
    # source rather than surfacing as a blank line on a rendered permit.
    contour_id = _required(application.contour_id, field="contour_id")
    activity_type_id = _required(application.activity_type_id, field="activity_type_id")
    contour_version_id = _required(application.contour_version_id, field="contour_version_id")
    area_ha = _required(application.requested_area_ha, field="area_ha")
    period_from = _required(application.period_from, field="period_from")
    period_to = _required(application.period_to, field="period_to")

    organization_id = await _assert_in_zone(db, actor, contour_id)

    calculation = await applications_service.current_calculation(db, application_id)
    if calculation is None:
        raise err("ERR-VAL-001", details={"reason": "no_calculation"})
    template = await repo.active_template(db, activity_type_id)
    if template is None:
        raise err(
            "ERR-VAL-001",
            details={
                "reason": "no_active_template",
                "activity_type_id": str(activity_type_id),
            },
        )
    layout_html = await _layout_html(db, template)

    # 4. The number. One UPDATE ... RETURNING under the row lock (ruling 9).
    series = get_settings().permit_series
    number = await repo.next_number(db, series)
    if number is None:
        # Configuration naming a series the database has no counter row for —
        # classically a Latin `A` where the seeded key is Cyrillic `А`. Refuse:
        # carrying on would write a permit with no number at all.
        raise err("ERR-SYS-001", details={"reason": "unknown_permit_series", "series": series})

    snapshot = await _snapshot(
        db,
        series=series,
        number=number,
        applicant_id=application.applicant_id,
        activity_type_id=activity_type_id,
        organization_id=organization_id,
        contour_id=contour_id,
        area_ha=area_ha,
        period_from=period_from,
        period_to=period_to,
        amount=calculation.amount,
        sb_load=calculation.used_sb,
        calculation_id=calculation.id,
        calculation_input=calculation.input_snapshot,
    )

    # 5. The QR token is a SECRET, not an identifier (ruling 8): never derived
    # from the series, the number or the id, and never returned in a response.
    qr_token = secrets.token_urlsafe(32)

    # 6. Rendering is blocking C code (Pango, HarfBuzz) — off the event loop, the
    # same rule 3.6a applies to `pyogrio`.
    pdf = await asyncio.to_thread(render.render_permit, snapshot, layout_html, qr_url(qr_token))

    # 7. The bytes, then their hash. `save_upload` writes to MinIO BEFORE the DB
    # flush on purpose: a storage failure aborts the transaction and leaves no
    # dangling row, while a dangling object is harmless garbage.
    document = await files.save_upload(
        db,
        data=pdf,
        filename=f"permit-{series}-{number:06d}.pdf",
        content_type="application/pdf",
        actor=actor,
    )

    permit = Permit(
        series=series,
        number=number,
        application_id=application.id,
        applicant_id=application.applicant_id,
        activity_type_id=activity_type_id,
        organization_id=organization_id,
        contour_id=contour_id,
        contour_version_id=contour_version_id,
        area_ha=area_ha,
        period_from=period_from,
        period_to=period_to,
        amount=calculation.amount,
        sb_load=calculation.used_sb,
        status=INITIAL_STATUS,
        pdf_file_id=document.id,
        doc_hash=hashlib.sha256(pdf).hexdigest(),
        qr_token=qr_token,
        template_id=template.id,
        snapshot=snapshot,
    )
    # 8. The row and its timeline.
    await repo.add(db, permit)
    await repo.add_status_history(
        db,
        PermitStatusHistory(
            permit_id=permit.id,
            from_status=None,
            to_status=INITIAL_STATUS,
            changed_by=actor.id,
        ),
    )
    # A plain INSERT's implicit RETURNING covers only what the DB generates, so a
    # caller-supplied value in a fixed-scale NUMERIC still reads back at the
    # posted scale until refreshed (lesson: the row in memory is not what Postgres
    # stored) — and this row is serialized straight into the response.
    await db.refresh(permit)

    # 9. NOT `set_status(..., PERMIT_ISSUED)`. See the docstring: ruling 18.

    # 10. The trail, in the same transaction as the action (the audit invariant).
    await audit.log(
        db,
        action=PERMIT_ISSUE,
        user_id=actor.id,
        object_type="permit",
        object_id=permit.id,
        new_value={
            "series": series,
            "number": number,
            "application_id": str(application.id),
            "doc_hash": permit.doc_hash,
            "template_id": str(template.id),
        },
    )

    # 11. The applicant is told. `owner_user_id` is the individual's own account;
    # a legal entity has none (decision #9), so the submitter — a representative
    # acting for it — is who hears about it.
    await notifications.notify(
        db,
        event_code=events.PERMIT_ISSUED,
        recipient_user_id=await _notification_recipient(
            db,
            applicant_id=application.applicant_id,
            submitted_by_user_id=application.submitted_by_user_id,
        ),
        params={
            "permit_number": _permit_number(series, number),
            "valid_from": period_from,
            "valid_to": period_to,
        },
        object_type="permit",
        object_id=permit.id,
    )
    return permit


async def _notification_recipient(
    db: AsyncSession, *, applicant_id: uuid.UUID, submitted_by_user_id: uuid.UUID
) -> uuid.UUID:
    """Who hears about this application's permit: the individual applicant's own
    account when there is one, otherwise whoever filed it. A legal entity has no
    `owner_user_id` (decision #9) and is reached through the representative who
    acted for it, which is also the fallback for an applicant row that has somehow
    lost its account — `notify` raises on a recipient it cannot resolve, and a
    permit must not fail to issue over a notification."""
    applicant = await auth_service.get_applicant(db, applicant_id)
    if applicant is not None and applicant.owner_user_id is not None:
        return applicant.owner_user_id
    return submitted_by_user_id


# --- the in-process read surface --------------------------------------------
#
# **The contract these two belong to is the closing comment block at the bottom
# of this file** — read that one, not this line, before calling anything here
# from another module. The two land here rather than there because issuance's own
# code above is their first caller, and both follow the rule every sibling read
# follows (`gis.service.published_version`, `applications.service.get`): no
# permission and no zone rule, because the caller is another SERVICE inside this
# process — the gates live on the routes that reach them.


async def for_application(db: AsyncSession, application_id: uuid.UUID) -> Permit | None:
    """The permit issued for this application, or None."""
    return await repo.permit_by_application(db, application_id)


async def pdf_bytes(db: AsyncSession, permit_id: uuid.UUID) -> bytes:
    """The stored document — the exact bytes `doc_hash` was taken over and every
    ERI signature covers.

    Reads storage directly rather than through `core.files.get_readable`, which
    applies the FILE subsystem's own rule: the uploader, or any non-applicant
    role. The uploader here is the issuing hodim, so `get_readable` would refuse
    the permit's own HOLDER their own permit — the opposite of the access rule
    this document needs. The download ROUTE (Task 8) applies the permit's rule;
    this function, like every read above it, applies none.
    """
    permit = await repo.permit_by_id(db, permit_id)
    if permit is None or permit.pdf_file_id is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})
    file = await db.get(MediaFile, permit.pdf_file_id)
    if file is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})
    return await storage.get_object(file.storage_key)


# --- Task 4: the 3+1 signatures ----------------------------------------------
#
# `tz/13`'s form carries four signature lines and the permit is legally real only
# when all four are on it (C11). Three invariants govern everything below.
#
# **The required set is data, not an `if`.** It lives in the admin-editable
# `permit_required_signatures` setting and is read through `signatures.service`
# (ruling 7) — never re-derived here, never counted here. This module asks that
# module what is still missing and believes the answer.
#
# **The document is never re-rendered.** Every signature is taken over
# `pdf_bytes` — the exact bytes `doc_hash` was frozen over at issuance (ruling
# 3) — so all four share one `doc_hash` by construction rather than by a check.
#
# **A permit becomes ACTIVE in exactly one place.** `_activate` below, reachable
# only from `add_signature` and only once `missing_purposes` has come back
# empty. Nothing else in this codebase may write that status.

OBJECT_TYPE = "permit"


def _permit_number(series: str, number: int) -> str:
    """The permit's number as every notification and the document itself print
    it. One formatter, three callers — a legal document's identifier must not be
    spelled two ways because two call sites each carried their own f-string."""
    return f"{series} № {number:06d}"


async def _is_holder(db: AsyncSession, permit: Permit, user: User) -> bool:
    """Whether `user` is the permit's own holder — the 4th signature line.

    Two ways, the same two `auth` already recognises everywhere else: the
    individual applicant's own account (`applicants.owner_user_id`), or an
    EFFECTIVE representation of a legal-entity applicant, judged against
    `business_today()` inside `auth.service` so a lapsed power of attorney stops
    working the day it lapses (decision #9: a legal entity has no account of its
    own and always acts through a representative).

    This is the half `signatures.sign()` structurally cannot check: it re-proves
    that the CERTIFICATE is the caller's own by PINFL/STIR, which says nothing
    about whether that caller is the holder of THIS permit. A different citizen
    signing with their own genuine key is crypto-valid and still a stranger.

    **One definition, three uses** (Task 8). The 4th signature line asks about one
    permit; `GET /permits/{id}` and `GET /permits/{id}/pdf` ask the same question
    about the same permit; `GET /permits` needs the SET, to build a query out of.
    A membership test over `auth.service.own_applicant_ids` is the only shape all
    three can share — a separate per-permit predicate beside a separate list scope
    is two rules that agree until one of them is edited, and the visible symptom
    would be a permit readable by id and missing from the list that must carry it.

    **What this delegation rests on.** It replaced an explicit
    `owner_user_id, else has_effective_representation(applicant.stir)` pair, and
    the two resolve the same set only because every `representations.applicant_id`
    points at a `kind='legal'` applicant, which `identity_by_kind` guarantees has
    a non-null `stir`. `representations` carries no DB CHECK on the target's KIND
    — the guarantee is the two writers in `auth.service`, `add_representation`
    (which refuses `kind != "legal"` outright) and `attach_legal`, staying the
    only ones. A third writer that could name an individual applicant would widen
    who may sign the recipient line, and this is the sentence that must be read
    before adding it.
    """
    return permit.applicant_id in await auth_service.own_applicant_ids(db, user.id)


async def _signer_refusal(
    db: AsyncSession, permit: Permit, *, purpose: str, user: User
) -> str | None:
    """`None` when `user` may sign `purpose` on `permit`; otherwise WHY not.

    One function decides both whether and why, the shape
    `signatures.service._ownership_reason` already uses — a bool-returning
    predicate plus a separately-maintained reason picker is two things that can
    disagree, and this one's reasons are read by stage 4.2's risk report.

    The reason is journal-only. The API answers `signer_not_authorized` and
    nothing finer: which of the four checks failed tells an attacker whether
    they guessed a real purpose, hold a signatory role, or merely sit in the
    wrong leshoz.

    Ruling 4, in the order the checks must run:

    1. **The recipient line** is proven by owning the application (`_is_holder`),
       never by holding a role: `applicant` is held by every citizen. It goes
       first because it is the one known purpose that maps to no role, so step 2
       would otherwise reject it as unknown.
    2. **A purpose that names no role is refused.** `permit_required_signatures`
       is admin-editable, so a typo must fail closed rather than silently create
       a slot anybody holding `permits.sign` could fill. This guard is not
       redundant with step 3: without it an unmapped purpose reaches the role
       comparison with `None` on one side, and a user whose own role row has
       gone missing would match it.
    3. **The three official lines** need the role AND the organization. Holding
       `executor_head` is not enough — `design/03` says users OF THE SAME
       ORGANIZATION, and a head of another leshoz signing this leshoz's permit
       is `tz/10`'s RI-12 («попытка доступа вне территориальных полномочий»).

    Strict equality on `users.organization_id`, deliberately NOT the three-axis
    `Zone` predicate `_assert_in_zone` uses for issuance. A zone answers "whose
    rows may I see", and a zone-free actor legitimately sees the whole republic;
    a signature answers "which named official of which named organization
    attests to this document", and there is no such thing as a republic-wide
    leshoz head. The same reasoning is why `sys_admin` — which
    `require_permission` waves through every gate (decision #41 ruling 2) — is
    refused here: a superuser bypass is about privilege, and this is identity.

    Today `permits.organization_id` is always the contour's leshoz, because
    nothing populates an organization below leshoz level yet (`organizations`
    can express `bolim`/`aylanma`/`bolak`, the Agency has delivered no such
    data). If those rows ever arrive, a head of the parent leshoz signing a
    sub-unit's permit needs a parent walk here, not a wider zone.
    """
    if purpose == signers.RECIPIENT_PURPOSE:
        return None if await _is_holder(db, permit, user) else "not_the_holder"
    role = signers.required_role(purpose)
    if role is None:
        return "unknown_purpose"
    # Compared against a non-None `role`, so a user whose own role row has
    # somehow gone missing (`auth.service.role_code` returns None for that) can
    # never match an unmapped purpose by both sides being None.
    if await auth_service.role_code(db, user) != role:
        return "wrong_role"
    if user.organization_id != permit.organization_id:
        return "wrong_organization"
    return None


async def _activate(db: AsyncSession, permit: Permit, *, actor: User) -> None:
    """The permit comes into force. **Call this from `add_signature` and nowhere
    else, and only once `signatures.service.missing_purposes` has come back
    empty** — C11 is that a permit is ACTIVE if and only if every required
    signature is valid, and a second writer of this status is how that stops
    being true.

    `issued_at` is stamped HERE, not at issuance. Two things are named alike and
    they are not the same moment: the date the DOCUMENT bears (`tz/13` §3, in the
    frozen snapshot) and the timestamp the permit became legally in force. Ruling
    18 is the same distinction on the application side — `tz/05` defines
    PERMIT_ISSUED as «сформировано **и подписано**», so the application moves
    here too, in this same step, and never at issuance.
    """
    permit.status = ACTIVE_STATUS
    permit.issued_at = datetime.now(UTC)
    await repo.add_status_history(
        db,
        PermitStatusHistory(
            permit_id=permit.id,
            from_status=INITIAL_STATUS,
            to_status=ACTIVE_STATUS,
            changed_by=actor.id,
        ),
    )
    # Ruling 18. `set_status` is the ONE way a level-4 module moves an
    # application (its own docstring): it validates PAID -> PERMIT_ISSUED against
    # tz/05, locks the row, writes the history entry and audits it.
    await applications_service.set_status(
        db, permit.application_id, to_status=APPLICATION_PERMIT_ISSUED, actor=actor
    )
    await notifications.notify(
        db,
        event_code=events.PERMIT_ACTIVE,
        recipient_user_id=await _holder_recipient(db, permit),
        params={
            "permit_number": _permit_number(permit.series, permit.number),
            "valid_from": permit.period_from,
            "valid_to": permit.period_to,
        },
        object_type=OBJECT_TYPE,
        object_id=permit.id,
    )


async def _notify_recipient_turn(db: AsyncSession, permit: Permit) -> None:
    """Ruling T4-a: tell the holder that everyone else has signed and the permit
    is now waiting on them.

    Sent at exactly one moment — when `missing_purposes` comes back as the
    recipient purpose ALONE — and therefore exactly once per permit: the missing
    set only ever shrinks (a `signatures` row is never updated, and `reverify`
    adds a row rather than removing a valid one), so it passes through that state
    at most once. Sending `permit.signed` on every signature instead would put
    four indistinguishable notices in a citizen's cabinet and four SMS parts on
    their phone, none of them naming an action.

    This is the only reminder 3.11a has. Nothing times out a citizen's signature
    — the plan leaves that to a later stage — so without it a paid application
    can sit in `pending_signatures` until its own period runs out, with the money
    already taken. `0019` seeds the wording to match: second person, and the SMS
    body carries the demand alone so it stays inside one 70-character part.

    A holder who signs FIRST never sees it, correctly: the missing set goes from
    four straight past this state, and there is nothing to remind them of.
    """
    await notifications.notify(
        db,
        event_code=events.PERMIT_SIGNED,
        recipient_user_id=await _holder_recipient(db, permit),
        params={"permit_number": _permit_number(permit.series, permit.number)},
        object_type=OBJECT_TYPE,
        object_id=permit.id,
    )


async def _holder_recipient(db: AsyncSession, permit: Permit) -> uuid.UUID:
    """Who hears about this permit — `_notification_recipient` over the pair of
    facts a permit always carries.

    One function rather than the two-call idiom repeated at each site: three
    callers now (`_activate`, `_notify_recipient_turn`, `jobs.expire_permits`),
    and a resolution rule copied three times is a rule that will hold in two
    places after somebody changes it."""
    return await _notification_recipient(
        db,
        applicant_id=permit.applicant_id,
        submitted_by_user_id=await _submitter_of(db, permit),
    )


async def _submitter_of(db: AsyncSession, permit: Permit) -> uuid.UUID:
    """Who filed the application this permit was issued for — the fallback
    recipient for a legal-entity applicant, which has no account of its own
    (decision #9). Read back from `applications` rather than copied onto the
    permit at issuance: a representation can change between paying and signing,
    and the person to tell is whoever the application says filed it now."""
    application = await applications_service.get(db, permit.application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(permit.application_id)})
    return application.submitted_by_user_id


async def add_signature(
    db: AsyncSession, permit_id: uuid.UUID, *, purpose: str, pkcs7: str, user: User
) -> Permit:
    """Attach one of the four ERI signatures, and activate the permit if it was
    the last one missing.

    The order below is the whole of the task and is load-bearing:

    1. the permit exists and is `pending_signatures`;
    2. `purpose` is in the configured requirement set;
    3. **authorization (ruling 4), BEFORE `sign()`;**
    4. `sign()` over the STORED bytes;
    5. nothing missing -> active, in one step with the application's own move;
    6. audit, always.

    **Why step 3 cannot come after step 4.** `uq_signatures_valid_purpose` is
    unique per `(object_type, object_id, purpose)` over VALID rows, so a wrong
    signer's valid signature occupies the slot permanently — discovering the
    mistake afterwards leaves a permit the right person can never sign, and
    `signatures` rows are evidence and are never deleted. Checking first costs a
    role lookup; checking last costs the document.

    Signatures may be taken in ANY order (plan ruling 5). `missing_purposes`
    returns an ordered list, but that is a display order for a signing UI, not a
    gate: enforcing a sequence would deadlock the ordinary case where the
    accountant is at their desk and the head is not.

    Nothing of ours is pending when `sign()` is called, deliberately: its
    transaction contract says every refusal commits the CALLER's whole session
    before raising, so a half-built change made before it would be committed by
    somebody else's rejected signature.
    """
    # LOCKED, not `permit_by_id` (review, fix round 1). Everything below —
    # the status check, `sign()`'s insert, `missing_purposes` and the activation
    # that depends on it — is one read-check-write over this permit, and two
    # signatories landing together would otherwise each miss the other's
    # uncommitted signature and leave the permit stuck with four valid
    # signatures and no activation. See `repo.permit_by_id_for_update`.
    permit = await repo.permit_by_id_for_update(db, permit_id)
    if permit is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})

    # 1. Only a permit awaiting signatures takes one. Without this the four
    # slots of an ACTIVE permit are already full, so `sign()` would answer
    # ERR-SIGN-002 "already signed" — true of the purpose, and the wrong
    # explanation for the permit; and a suspended or revoked permit would go on
    # collecting signatures as if nothing had happened (3.11b owns those).
    if permit.status != INITIAL_STATUS:
        raise err(
            "ERR-PERM-001",
            details={"reason": "not_pending_signatures", "status": permit.status},
        )

    # 2. A purpose outside the requirement set could never satisfy anything.
    # `sign()` refuses it too, in the same words — refusing here keeps the reason
    # accurate, because the check below would otherwise report a made-up purpose
    # as an unauthorized signer rather than as a purpose nobody asked for.
    required = await signatures_service.required_purposes(db, OBJECT_TYPE)
    if purpose not in required:
        raise err("ERR-SIGN-001", details={"reason": "purpose_not_required"})

    # 3. Ruling 4 — the check stage 3.8 documented and left here. Early commit on
    # denial (decision #40): the raise would otherwise roll the trail back
    # together with the very exception it exists to explain.
    refusal = await _signer_refusal(db, permit, purpose=purpose, user=user)
    if refusal is not None:
        await audit.log(
            db,
            action=PERMIT_SIGN,
            user_id=user.id,
            object_type=OBJECT_TYPE,
            object_id=permit.id,
            result="denied",
            basis="signer_not_authorized",
            new_value={"purpose": purpose, "reason": refusal},
            # tz/10 RI-12 is «попытка доступа вне территориальных полномочий» —
            # High, immediate. The right role in the wrong leshoz is exactly
            # that; a wrong role in the right leshoz is an ordinary refusal.
            extra={"risk_indicator": "RI-12"} if refusal == "wrong_organization" else None,
        )
        await db.commit()
        raise err("ERR-ACL-001", details={"reason": "signer_not_authorized"})

    # 4. The stored bytes, never a re-render (ruling 3). This is what makes the
    # four signatures share one `doc_hash` by construction.
    document = await pdf_bytes(db, permit.id)
    await signatures_service.sign(
        db,
        object_type=OBJECT_TYPE,
        object_id=permit.id,
        purpose=purpose,
        document=document,
        pkcs7=pkcs7,
        user=user,
    )

    # 5. C11: ACTIVE if and only if every required signature is valid — asked of
    # `signatures`, never counted here.
    missing = await signatures_service.missing_purposes(
        db, object_type=OBJECT_TYPE, object_id=permit.id
    )
    if not missing:
        await _activate(db, permit, actor=user)
    elif missing == [signers.RECIPIENT_PURPOSE]:
        # Ruling T4-a — the holder is the only one left, and nothing else in this
        # stage will ever tell them so. See `_notify_recipient_turn`.
        await _notify_recipient_turn(db, permit)

    # 6. The trail, in the same transaction as the action (the audit invariant).
    await audit.log(
        db,
        action=PERMIT_SIGN,
        user_id=user.id,
        object_type=OBJECT_TYPE,
        object_id=permit.id,
        new_value={"purpose": purpose, "status": permit.status, "missing": missing},
    )
    return permit


# --- Task 6: the two provider seams gis (3.6a) and norms (3.7) left open -----
#
# Both seams were opened with nothing behind them and an explicit placeholder in
# front — `occupancy_source: "none"`, `load_source: "none"` — so that no reader
# could take a zero for a measurement until this module existed. Registering the
# two functions below (in `app/event_subscriptions.py`, which BOTH entry points
# call) is what flips them to `"permits"` across the whole system.
#
# Neither takes a permission or a zone rule, like every other in-process read
# here: the caller is another SERVICE, and the gates live on the routes above it.
#
# `ACTIVE_STATUS` and nothing else. A `suspended` permit is not in use, so it
# occupies no hectare and commits no head; `expired`/`revoked` are over and
# `pending_signatures` has not begun (C11: a permit is in force only once all
# four signatures are on it). That single word is the whole of ruling 11, and it
# is passed down to the repo rather than repeated there.


async def occupancy_provider(
    db: AsyncSession, contour_ids: Sequence[uuid.UUID]
) -> Mapping[uuid.UUID, Decimal]:
    """How much of each contour's area is taken by permits in force.

    ONE query for the whole page, never one per contour: 3.6a reshaped this seam
    from per-contour to batch specifically so that this, its first registration,
    could not turn a page of 20 into 20 round-trips (~13,500 for the
    whole-country list, by the seam's own comment). An empty page costs no query
    at all — `gis.service.list_contours` calls the seam for every page including
    one that matched nothing, and `IN ()` is a statement with no possible answer.

    A contour with nothing on it is absent from the mapping rather than present
    as a zero; the seam's contract is that a key it does not get back counts as
    zero, and `occupancy_map` quantizes whatever it is given to `area_ha`'s own
    NUMERIC(12,4) scale.
    """
    if not contour_ids:
        return {}
    return await repo.occupied_area_by_contour(db, contour_ids, status=ACTIVE_STATUS)


async def load_provider(
    db: AsyncSession, contour_id: uuid.UUID, period_from: date, period_to: date
) -> Decimal:
    """Conditional heads (шартли бош) already committed on this contour for a
    period overlapping `[period_from, period_to]` — what `norms` subtracts from a
    contour's `max_sb` before pricing one more herd onto it.

    Sums `permits.sb_load`, the figure frozen at issuance, never a recomputation
    from the herd: the permit's own snapshot is the record of what was allowed
    (`tz/05` invariant 7), and re-deriving it here would let a later tariff
    regrouping change how much room a contour has today.
    """
    return await repo.committed_sb_load(
        db, contour_id, period_from, period_to, status=ACTIVE_STATUS
    )


async def missing_signatures(db: AsyncSession, permit_id: uuid.UUID) -> list[str]:
    """Which of the required purposes this permit still lacks a VALID signature
    for, in the configured order. A pass-through to `signatures.service` on
    purpose: the requirement set is that module's to own (ruling 7), and a
    second place deciding it is how a permit ends up ACTIVE with three
    signatures."""
    return await signatures_service.missing_purposes(
        db, object_type=OBJECT_TYPE, object_id=permit_id
    )


# --- Task 5: the anonymous public check (С12) --------------------------------
#
# No `require_permission`, no `get_current_user`, and — deliberately — no
# `audit.log`. The audit invariant covers state-changing ACTIONS by an actor;
# this is a read by nobody, and an `audit_log` row per anonymous check would be
# precisely the trail `design/02` forbids for `qr_check_log` ("no IP addresses
# and no personal data"). The `qr_check_log` row IS the record of the call.

# `tz/04` С12 and `design/03`, verbatim and in Uzbek Cyrillic: the four words
# this page may print, mapped from `permits.status`. 3.11a only ever produces
# `active` and `expired` — `suspended` and `revoked` are 3.11b's — but the map is
# complete now, because it is what 3.11b lands on rather than something it has to
# invent alongside its transitions.
PUBLIC_STATUS_LABELS: dict[str, str] = {
    "active": "амалда",
    "suspended": "тўхтатилган",
    "expired": "муддати тугаган",
    "revoked": "бекор қилинган",
}

# The two statuses that are NOT public, each for its own reason — spelled out
# rather than left to fall through the map above, so that adding a seventh
# permit status forces a decision instead of silently answering "no such
# permit". `test_public_check.py` asserts the two sets together cover
# `PERMIT_STATUSES` exactly.
#
#   * `pending_signatures` — the QR page goes live when the permit does. The
#     document exists and is already printable, but nobody has signed it (C11),
#     and a page showing it as a permit would make an unsigned draft read as one
#     in force.
#   * `archived` — 4.7's. `tz/05` reaches ARCHIVED from REVOKED **and** from
#     EXPIRED, so the status column no longer says which of the two words is
#     true, and this page must not guess: «муддати тугаган» on a permit that was
#     cancelled for cause is a false statement about a legal ground on a state
#     verification page. The stage that starts writing `archived` decides — keep
#     the legal status beside it, or give the page a fifth word — and until then
#     an archived permit is answered like any other lookup with nothing in force
#     to show. Noted as a question for that stage rather than settled here.
NON_PUBLIC_STATUSES: tuple[str, ...] = ("pending_signatures", "archived")

# `qr_check_log.channel` and `.result`. The tuples in `models.py` build the DB
# CHECKs and stay the single source of truth; these four names are what the code
# reads, and the test closes the gap with a set equality (lesson).
CHANNEL_QR = "qr"
CHANNEL_MANUAL = "manual"
RESULT_FOUND = "found"
RESULT_NOT_FOUND = "not_found"

# Which settings row budgets which channel (review I1). The two live apart
# because the spaces they defend are not alike: a token is unguessable, a
# series and number are gapless.
CHANNEL_RATELIMITS = {
    CHANNEL_QR: "ratelimit_public_check_qr_per_minute",
    CHANNEL_MANUAL: "ratelimit_public_check_manual_per_minute",
}

# The masked middle of a name. Fixed width on purpose: a mask as long as what it
# hides would report the surname's length, which is most of a surname.
NAME_MASK = "***"
# Below this, the ending is dropped and only the initial survives: at four
# characters, first-plus-last-two would hide exactly one letter, and at two it
# would print the whole name with asterisks in front of it.
NAME_MASK_MIN_LENGTH = 5

# Bounds on what the query string may carry. Neither is a business rule — they
# keep an anonymous caller from handing the database a value it cannot compare:
# `permits.number` is `bigint`, and an integer past that raises out of asyncpg as
# a 500 rather than a miss (lesson: an anonymous route must cap its inputs).
MAX_PERMIT_NUMBER = 2**63 - 1
# `qr_token` is `secrets.token_urlsafe(32)`, 43 characters; the series is one
# Cyrillic letter today (`'А'`). Both caps are generous and still bounded.
QR_TOKEN_MAX_LENGTH = 128
SERIES_MAX_LENGTH = 8


def mask_name(full_name: str) -> str:
    """`tz/04` С12: «Персональные данные — маскированы (ФИО сокращённо)».

    «Азизов Азиз Азизович» becomes «А.***ов А.» — `design/03`'s own example. The
    surname keeps its initial and its last two letters, which is what lets
    someone who already knows the holder recognise them; everything between is
    three asterisks whatever its length. The given name is reduced to an initial
    and the patronymic is dropped entirely: one initial is enough to confirm, and
    every further character is one more given away by a page with no login.

    Total by design, never raising and never returning an empty string — it runs
    on an unauthenticated route, and `str.split()` on a blank name yields no
    parts at all (answered with `NOT_STATED`, the form's own em dash).
    """
    parts = full_name.split()
    if not parts:
        return NOT_STATED
    surname = parts[0]
    if len(surname) < NAME_MASK_MIN_LENGTH:
        masked = f"{surname[0]}.{NAME_MASK}"
    else:
        masked = f"{surname[0]}.{NAME_MASK}{surname[-2:]}"
    return f"{masked} {parts[1][0]}." if len(parts) > 1 else masked


def _from_snapshot(snapshot: Any, key: str) -> str:
    """One requisite of the frozen document, or `""` when it holds none.

    A key missing from an issued permit's snapshot is a defect on the ISSUING
    side (`_snapshot` builds all of them through `_required`), and this route is
    the wrong place to discover it: a `ResponseValidationError` here would be a
    500 on the one page a citizen with a paper permit can reach. The caller turns
    the empty string into the form's em dash, exactly as `_holder_address` does
    for a registry that holds nothing.

    Empty rather than `NOT_STATED` directly, because one caller masks its value
    first: `mask_name("—")` would print «—.***», a mask of a placeholder.
    """
    value = snapshot.get(key) if isinstance(snapshot, dict) else None
    return "" if value is None else str(value)


def check_channel(
    *, qr_token: str | None = None, series: str | None = None, number: int | None = None
) -> str:
    """Which channel a lookup is, decided ONCE for the rate limit and the log.

    The router has to know before the service runs — the two channels have
    separate budgets (review I1) — and the log has to record the same answer, so
    a second reading of the same three parameters is a second thing that can
    disagree. The token wins when both are given: it is the stronger claim (a
    scanner read it off the document).

    `ERR-VAL-001` when nothing is named. That refusal is deliberately cheap and
    costs no token: nothing was looked up, so there is nothing to budget, and
    the refusal is about the shape of the request rather than about whether any
    permit exists.
    """
    if (qr_token or "").strip():
        return CHANNEL_QR
    if (series or "").strip() and number is not None:
        return CHANNEL_MANUAL
    raise err("ERR-VAL-001", details={"reason": "qr_or_series_and_number"})


async def public_check(
    db: AsyncSession,
    *,
    qr_token: str | None = None,
    series: str | None = None,
    number: int | None = None,
) -> dict[str, Any]:
    """The anonymous verification card, and the statistics row that counts it.

    **A miss is not an error.** An unknown token, an unknown number and a permit
    that is not public yet all answer the same `{"found": False}`: an endpoint
    with no authentication that answered 404 for one and 200 for the other would
    be a free permit-number oracle, and `?series=&number=` is guessable by
    construction (ruling 8). The rate limit on the route is what actually bounds
    enumeration.

    **The card is read off the frozen snapshot**, never off `applicants` or
    `organizations`. Two reasons, and the second is the load-bearing one: this
    page verifies a PRINTED document, so it must say what the paper says — a
    holder who renames themselves tomorrow would otherwise make a genuine permit
    read as a forgery to the inspector comparing the two. And an anonymous route
    that joined into `applicants` would be reading personal data live to publish
    three characters of it. The permit's OWN columns (`status`, the period) come
    from the row, because they are not copies of anything mutable — `status` is
    requisite 25, deliberately excluded from the snapshot precisely because it
    changes over the permit's life.

    **`signatures_valid` is the stored verdict, and it is a question about
    HISTORY** (ruling T5-a). It asks whether every signature this permit was
    activated with is still recorded valid — never whether the CURRENT
    `permit_required_signatures` is satisfied. The two differ on purpose and
    must not be "fixed" to match: the activation gate in `_activate` reads the
    live setting through `signatures.service` because that is what ruling 6
    requires of a gate, while this read must not, because the setting is
    admin-editable and `tz/04` С11's «все три обязательны?» is still open with
    the Agency. Re-deriving here would mean that the day the Agency answers,
    every permit issued before it starts telling inspectors its signatures do
    not check out — the worst thing this page can say about a lawful document.

    Nothing here calls E-IMZO, which at 5.2 lives behind a VPN reachable only
    from inside Uzbekistan. A certificate that expires in 2029 does not
    retroactively invalidate a permit lawfully signed in 2026 — but an
    oversight `reverify` that finds it REVOKED writes an invalid verdict
    against that signature, and this field then reads false while the permit's
    own status is untouched. That is 3.11b's to act on.

    **The log records what was ANSWERED**, not what the SELECT found: a permit
    refused as not-public is logged `not_found` with a null `permit_id`, which
    keeps `models.py`'s invariant («that null IS the not_found case's whole
    payload») true and keeps the statistics from recording that somebody looked
    at a specific unsigned document.
    """
    # One decider for the channel, shared with the router's rate limit.
    channel = check_channel(qr_token=qr_token, series=series, number=number)
    if channel == CHANNEL_QR:
        permit = await repo.permit_by_qr_token(db, (qr_token or "").strip())
    else:
        permit = await repo.permit_by_series_number(db, (series or "").strip(), number or 0)

    label = None if permit is None else PUBLIC_STATUS_LABELS.get(permit.status)
    if permit is None or label is None:
        await repo.add(db, QrCheckLog(permit_id=None, result=RESULT_NOT_FOUND, channel=channel))
        return {"found": False}

    await repo.add(db, QrCheckLog(permit_id=permit.id, result=RESULT_FOUND, channel=channel))
    return {
        "found": True,
        "status": label,
        "valid_from": permit.period_from,
        "valid_to": permit.period_to,
        "organization": _from_snapshot(permit.snapshot, "leshoz_name") or NOT_STATED,
        "activity_type": _from_snapshot(permit.snapshot, "activity_name") or NOT_STATED,
        "signatures_valid": await signatures_service.carried_signatures_valid(
            db, object_type=OBJECT_TYPE, object_id=permit.id
        ),
        # `mask_name` answers `NOT_STATED` on an empty name itself, so there is no
        # `or` here: a mask applied to the em dash would print «—.***».
        "holder": mask_name(_from_snapshot(permit.snapshot, "holder_name")),
    }


# --- Task 8: the public surface for levels 4+ and stage 4 --------------------
#
# **This block is the contract `inspections` (4.1), `oversight` (4.2), `archive`
# (4.7) and 3.11b build against, and it does not change after this task.**
#
# SEVEN entry points, and nothing else:
#
# - `get(db, permit_id) -> Permit | None` — the permit row, or None. No
#   permission and no zone rule: the caller is another SERVICE inside this
#   process, mirroring `gis.service.published_version`,
#   `norms.service.effective_norm` and `applications.service.get`. `None` rather
#   than a raise, because a missing permit means different things to different
#   callers — an inspection act without one is a defect, an archival sweep's is
#   simply nothing to do.
# - `for_application(db, application_id) -> Permit | None` — the 1:1 partner of
#   an application (`permits.application_id` is unique). This is how a caller
#   holding an application id reaches its permit; there is no other way, and
#   there must not be a JOIN of the two tables written anywhere else.
# - `pdf_bytes(db, permit_id) -> bytes` — the STORED document, never a
#   re-render (ruling 3). These are the exact bytes `doc_hash` was taken over
#   and all four ERI signatures cover, so re-rendering would silently produce a
#   document no signature verifies against. `ERR-SYS-003` when the permit or its
#   file is missing.
# - `set_status(db, permit_id, *, to_status, actor=None, reason=None,
#   reason_item_id=None, doc_file_id=None, correlation_id=None) -> Permit`
#   — **the ONE way a module OUTSIDE `permits` moves a permit**: 3.11b writes
#   `suspended`/`revoked` (and `suspended -> active` on resume), 4.7 writes
#   `archived`. See its own docstring for what it does and does not do.
# - `occupancy_provider(db, contour_ids) -> Mapping[uuid, Decimal]` and
#   `load_provider(db, contour_id, period_from, period_to) -> Decimal` (Task 6)
#   — the two seams `gis` and `norms` registered in `app/event_subscriptions.py`.
#   Callable directly as well; both count `active` permits and nothing else.
# - `missing_signatures(db, permit_id) -> list[str]` — which required purposes
#   still lack a valid signature, in the configured display order. A pass-through
#   to `signatures.service` on purpose (ruling 7).
#
# A caller above this module must NEVER:
#   - **import `permits.repo` or `permits.models`.** Every fact reachable that
#     way is already answered above, and the two exceptions the boundary rule
#     grants — `gis`/`norms` reading permits, and the level-5 readers (reports,
#     dashboard, search, oversight, archive) — are read-only table access for
#     AGGREGATES, never a second write path;
#   - **`UPDATE permits.status` directly.** `set_status` exists so that every
#     transition in the system has one implementation, one validated edge, one
#     `permit_status_history` row and one audit entry. A direct UPDATE produces a
#     permit whose timeline does not explain it — and `permit_status_history` is
#     append-only at the database level, so the missing row cannot be added
#     afterwards;
#   - **re-derive whether a permit is in force.** `status == "active"` is the
#     whole answer (C11), already decided by `_activate` against
#     `signatures.service`; counting signature rows in a second place is how a
#     permit ends up "valid" for one screen and not for another;
#   - **read `permits.snapshot` as live data.** It is frozen at issuance
#     (`tz/05` invariant 7) and is what the PDF says. A name or a tariff read
#     from it is the name and tariff of the day the document was signed, which
#     is exactly right for verification and exactly wrong for a report.
#
# `set_status` CARRIES EVERY COLUMN `permit_status_history` HAS, so that "the one
# way" is not immediately contradicted by the caller who needs it most (ruling
# T8-b). Besides `to_status`, `actor` and `reason` it takes `reason_item_id` and
# `doc_file_id` — a suspension's legal ground: the classifier item and the order
# that carries it (С13, 3.11b) — and `correlation_id`, so a sweep's rows all
# carry the run they came from. All five default to None, so no caller of the
# narrower form changes. Widening was free exactly once, here, before anything
# depended on the shape; the freeze exists to stop LATER changes, and a
# suspension recording its ground writes two more columns of the same history row
# this function already writes rather than doing different work.
#
# THE FIRST WRITER INSIDE THIS MODULE THAT STILL DOES NOT ROUTE THROUGH IT is
# `_activate`, exactly as `applications`' own `submit` writes its own transition
# rather than routing through that module's `set_status` (its ruling 25): it
# stamps `issued_at`, moves the APPLICATION and notifies the holder in one step,
# which is a flow verb, not a status move. `jobs.expire_permits` is the second,
# and for a narrower reason — it drains its candidates in batches, each row
# inside its own SAVEPOINT so a raising permit costs itself instead of its batch,
# and that structure owns the write. Both edges are asserted against
# `PERMIT_TRANSITIONS` in `test_end_to_end.py`, so the table describes the code
# rather than merely sitting beside it, and neither writer may add an edge the
# table does not have.
#
# WHAT IS PUBLIC IN THIS FILE AND STILL NOT PART OF THE CONTRACT, so that
# "seven entry points" above means what it says. Everything else here belongs to
# one of this module's OWN entry points and takes an HTTP actor or a scheduler,
# not a sibling service: `issue` and `add_signature` (its two write routes),
# `permit_card`/`list_permits`/`permit_document` (its three read routes, below)
# and `public_check`/`check_channel`/`mask_name` (the anonymous QR page). Calling
# any of them from another module would import that module's actor semantics
# along with the data; `jobs.py`'s two sweeps are likewise the scheduler's alone.
#
# THE THREE READ ROUTES below (`GET /permits`, `/permits/{id}`,
# `/permits/{id}/pdf`) are that HTTP surface. Their access rule is "the holder,
# or staff holding `permits.view_any` within their zone" — BOTH halves, because
# a permission answers "may this role at all" and a zone answers "on whose rows"
# (lesson).


async def get(db: AsyncSession, permit_id: uuid.UUID) -> Permit | None:
    """The permit row, or `None`. No permission and no zone rule: the caller is
    another SERVICE inside this process (see the block above)."""
    return await repo.permit_by_id(db, permit_id)


def _assert_transition(permit: Permit, to_status: str) -> None:
    """A transition not listed in `PERMIT_TRANSITIONS[permit.status]` is a
    conflict with the permit's CURRENT state, not a malformed request body —
    `ERR-PERM-001` (409), never `ERR-VAL-001`. The same shape
    `applications.service._assert_transition` and `gis.service._assert_transition`
    already have over their own tables."""
    if to_status not in PERMIT_TRANSITIONS[permit.status]:
        raise err(
            "ERR-PERM-001",
            details={"reason": "bad_transition", "from": permit.status, "to": to_status},
        )


async def set_status(
    db: AsyncSession,
    permit_id: uuid.UUID,
    *,
    to_status: str,
    actor: User | None = None,
    reason: str | None = None,
    reason_item_id: uuid.UUID | None = None,
    doc_file_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> Permit:
    """Move a permit from one `tz/05` status to another: validate the edge, write
    the `permit_status_history` row, audit it, return the permit.

    Touches ONLY `permits.status` on the permit itself. `issued_at` belongs to
    `_activate`, which is the one place a permit comes into force; anything else
    a future transition needs to write on the PERMIT belongs to the flow verb
    that owns it, never here.

    **The three arguments past `reason` are the whole of ruling T8-b**, and they
    are why "the one way a module outside `permits` moves a permit" is a rule
    rather than a slogan a caller has to break:

      * `reason_item_id` — the `classifier_items` row naming WHY (С13's grounds
        for a suspension or a revocation). `reason` beside it is free text and
        the two are not alternatives: the code is what a report can group by, the
        text is what a citizen reads.
      * `doc_file_id` — the order that carries that ground, so the history row
        points at the document instead of merely asserting it exists.
      * `correlation_id` — passed straight to `audit.log`, so a sweep's rows
        carry the run they came from. Absent, `audit.log` picks up the request id
        the correlation middleware bound; a worker has no request and must pass
        its own.

    None of the three is validated here, deliberately: an FK does that at the
    database, and a service-level existence check would be a second opinion that
    can only ever be more permissive than the constraint. The caller supplying
    them is inside this module (3.11b) or a level-4 module with its own route.

    Enforces NO permission and NO zone rule of its own — the same design as every
    other function on this page, and for the same reason
    `applications.service.set_status` gives: the calling module's ROUTE is what
    holds the permission, and a check here could not span two different modules'
    permission codes (`permits.manage` for 3.11b's revoke, an archival grant for
    4.7) without inventing a third.

    **Locks the permit for the length of the call** (`repo.permit_by_id_for_update`
    — `FOR UPDATE` plus `populate_existing`, which are one mechanism and not two:
    without the repopulate the loader keeps whatever an already-held instance was
    carrying and a stale status is validated under a correct lock). This is the
    single write path several future callers share — 3.11b's revoke and 4.7's
    archival can genuinely arrive together — and without the lock both would read
    the same pre-write status, both pass `_assert_transition`, and the second
    UPDATE would silently overwrite the first, leaving a timeline claiming two
    transitions out of a status the permit was only in once.

    **Lock order: the permit, and never an application after it here.**
    `add_signature` takes a permit lock and then an application lock through
    `applications.service.set_status`, and that is the only lock ordering
    anywhere in `app/`. This function takes the permit lock alone, so it cannot
    close a cycle — a future caller that needs to move both must do so in that
    same order.

    Refuses an illegal jump with `ERR-PERM-001` and
    `details = {"reason": "bad_transition", "from": ..., "to": ...}`. A repeat
    call for the status the permit already holds lands there too, with
    `from == to`, which is how a retrying caller tells "already applied,
    harmless" from a genuine mistake.
    """
    permit = await repo.permit_by_id_for_update(db, permit_id)
    if permit is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})

    _assert_transition(permit, to_status)
    from_status = permit.status
    permit.status = to_status

    await repo.add_status_history(
        db,
        PermitStatusHistory(
            permit_id=permit.id,
            from_status=from_status,
            to_status=to_status,
            # Nullable on purpose: `expired` is the nightly sweep's and
            # `archived` will be 4.7's, and neither has a person behind it.
            changed_by=actor.id if actor else None,
            legal_basis=reason,
            reason_item_id=reason_item_id,
            doc_file_id=doc_file_id,
        ),
    )
    # `updated_at` carries `onupdate=func.now()`, which SQLAlchemy leaves EXPIRED
    # after a plain UPDATE — reading it outside the session's async context then
    # raises `MissingGreenlet` (lesson: the row in memory is not what Postgres
    # stored). The caller gets the row back, so it is refreshed here.
    await db.refresh(permit)

    await audit.log(
        db,
        action=PERMIT_STATUS_CHANGE,
        user_id=actor.id if actor else None,
        object_type=OBJECT_TYPE,
        object_id=permit.id,
        old_value={"status": from_status},
        new_value={"status": to_status},
        basis=reason,
        correlation_id=correlation_id,
        # The trail says WHICH ground was cited, not just that one was: `basis`
        # above is the free text, and a report grouping suspensions by cause
        # needs the code. `str(...)`, because `audit_log.extra` is JSONB and
        # nothing in this app configures a JSON encoder for `UUID` (lesson).
        extra=None if reason_item_id is None else {"reason_item_id": str(reason_item_id)},
    )
    return permit


# --- the three read routes' service side -------------------------------------
#
# A SUCCESSFUL read is not audited: the audit invariant covers state-changing
# actions, and a row per GET would let anyone holding a session write the trail
# at will. A read DENIED on territory is audited (ruling T8-a) — see
# `_readable_permit`, which is the one place both direct-access routes reach it.


async def _holds_view_any(db: AsyncSession, actor: User) -> bool:
    """Holds `permits.view_any`, or is the superuser that passes every permission
    gate (decision #41 ruling 2) — the same two-branch shape
    `signatures.service._holds_view_any`, `norms.service._holds_tariffs_publish`
    and `gis.service._may_manage_layers` use for a rule checked INSIDE a handler.

    It cannot be a route-level `require_permission` dependency: these routes also
    admit the permit's own HOLDER, who holds no such grant, so the dependency
    would reject a citizen reading their own permit before the ownership check
    ever ran (the same reason `GET /signatures` puts its check here).
    """
    if await auth_service.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    return PERMITS_VIEW_ANY in await auth_repo.permission_codes(db, actor)


async def _readable_permit(db: AsyncSession, permit_id: uuid.UUID, *, actor: User) -> Permit:
    """The permit `actor` is allowed to read, or a refusal.

    Two refusals, on purpose, and the difference is what the caller already
    knows:

      * a caller who is neither the holder nor a `permits.view_any` holder gets
        `ERR-SYS-003` (404) — the same answer an id that never existed gets.
        Anything else is a permit-existence oracle: a citizen could learn which
        ids are real by which ones answer 403;
      * a `permits.view_any` holder outside the permit's zone gets `ERR-ACL-002`
        (403). This one is staff, the refusal IS territorial, and saying so is
        what `_assert_organization_in_zone` says on the issuance path for the
        same actor and the same leshoz.

    The holder is checked FIRST so a citizen never depends on the zone: an
    applicant carries no zone at all, and staff who also happen to hold a permit
    of their own in another leshoz read it as its holder.

    **The territorial refusal writes an RI-12 trail before it raises** (ruling
    T8-a, `tz/10`: «попытка доступа вне территориальных полномочий», High,
    immediate), through the early-commit-on-denial pattern decision #40 ruling 2
    fixes for exactly this — the raise would otherwise roll the trail back
    together with the very exception it exists to explain. The same actor
    attempting to SIGN a permit outside their zone is already recorded
    (`_signer_refusal`'s `wrong_organization`); without this, stage 4.2's risk
    report would see who tried to sign one and miss who tried to look at one.

    **RI-12 coverage on reads is DIRECT ACCESS ONLY, and 4.2 needs to know it.**
    `GET /permits` cannot produce a territorial denial at all — nobody named a
    target, so `zone_filter` simply returns fewer rows and there is no attempt to
    record. This function is the whole of the indicator's read-side surface:
    `GET /permits/{id}` and `GET /permits/{id}/pdf`, both of which come through
    here. A probe that walks the LIST is invisible to RI-12 by construction and
    is the rate limiter's problem, not the audit trail's.

    The other refusal is NOT audited, deliberately: a caller who holds no
    `permits.view_any` cannot be "outside their zone" — they have no zone claim
    to exceed — and a row per 404 would let any signed-in citizen fill
    `audit_log` by guessing uuids.
    """
    permit = await repo.permit_by_id(db, permit_id)
    if permit is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})
    if await _is_holder(db, permit, actor):
        return permit
    if not await _holds_view_any(db, actor):
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})
    if not await _organization_in_actor_zone(db, actor, permit.organization_id):
        await audit.log(
            db,
            action=PERMIT_READ,
            user_id=actor.id,
            object_type=OBJECT_TYPE,
            object_id=permit.id,
            result="denied",
            basis="out_of_zone",
            extra={"risk_indicator": "RI-12"},
        )
        await db.commit()
        raise err("ERR-ACL-002")
    return permit


async def permit_card(db: AsyncSession, permit_id: uuid.UUID, *, actor: User) -> dict[str, Any]:
    """`GET /permits/{id}` — the permit, its signatures and its timeline.

    The signature list comes from `signatures.service`, never from a query of
    `signatures` here: that module owns the requirement set (ruling 7) and a
    second reader of its table is a second opinion about what "signed" means.
    `missing_signatures` rides along so a signing screen needs one request rather
    than two, and it is what makes the card self-explanatory while a permit is
    still `pending_signatures`.
    """
    permit = await _readable_permit(db, permit_id, actor=actor)
    return {
        "permit": permit,
        "signatures": await signatures_service.get_for_object(
            db, object_type=OBJECT_TYPE, object_id=permit.id
        ),
        "history": await repo.status_history(db, permit.id),
        "missing_signatures": await missing_signatures(db, permit.id),
    }


async def permit_document(
    db: AsyncSession, permit_id: uuid.UUID, *, actor: User
) -> tuple[Permit, bytes]:
    """`GET /permits/{id}/pdf` — the permit's access rule, then its stored bytes.

    Deliberately NOT `core.files.get_readable`: the file subsystem's own rule is
    "the uploader, or any non-applicant role", and the uploader here is the
    issuing hodim — so that rule would refuse the permit's own HOLDER their own
    permit, the exact opposite of what this document needs (`pdf_bytes`'s own
    docstring). The permit's rule is applied here instead, and the bytes come
    back unchanged from storage.
    """
    permit = await _readable_permit(db, permit_id, actor=actor)
    return permit, await pdf_bytes(db, permit.id)


async def list_permits(
    db: AsyncSession,
    *,
    actor: User,
    params: PageParams,
    status: str | None = None,
    applicant_id: uuid.UUID | None = None,
    contour_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    series: str | None = None,
    number: int | None = None,
) -> tuple[list[Permit], int]:
    """`GET /permits` — one page of the permits `actor` may see, plus the total.

    The scope is the UNION of the two things `_readable_permit` admits one at a
    time, so the list can never disagree with the card: the caller's own permits
    (`auth.service.own_applicant_ids`, the same set `_is_holder` tests membership
    in), OR — for a `permits.view_any` holder — everything inside their zone. A
    republic-wide staff member's `zone_filter` is `true()` and they see the lot;
    a zone-scoped one sees their own leshoz, and a permit outside it is simply
    absent rather than refused, because a filter has no way to answer 403.

    A caller who is neither gets `([], 0)` and no statement is issued: an empty
    scope must mean "nothing", and building it as a WHERE clause would leave one
    editing mistake between here and "every permit in the country".

    All three zone axes are supplied. `Permit` carries `organization_id` only, so
    `repo.list_permits` joins `organizations` for the other two — `zone_filter`
    fails closed and RAISES when an axis is set without its column, and a
    region-scoped, organization-less actor is creatable today
    (`admin.users_service.create_user` sets the three independently).

    `series` and `number` filter independently and AND together, so `?series=А`
    alone lists a whole series and the pair — unique by
    `uq_permits_series_number` — resolves to at most one row.
    """
    scope: list[Any] = []
    holder_ids = await auth_service.own_applicant_ids(db, actor.id)
    if holder_ids:
        scope.append(Permit.applicant_id.in_(holder_ids))
    if await _holds_view_any(db, actor):
        scope.append(
            zone_filter(
                zone_of(actor),
                region_col=Organization.region_id,
                district_col=Organization.district_id,
                organization_col=Permit.organization_id,
            )
        )
    if not scope:
        return [], 0
    return await repo.list_permits(
        db,
        scope=or_(*scope),
        status=status,
        applicant_id=applicant_id,
        contour_id=contour_id,
        organization_id=organization_id,
        series=series,
        number=number,
        offset=params.offset,
        limit=params.page_size,
    )
