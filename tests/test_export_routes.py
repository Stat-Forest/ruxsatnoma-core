"""Stage 13 guard (ruling #204): every paged register either serves its
`export.xlsx` sibling or is named in the backlog below.

A new list route lands in exactly one of the three sets — `EXPORTED`,
`EXPORT_BACKLOG` or `EXEMPT` — or this test says which one it forgot. The
backlog is emptied by the coordinator as the stage-13 tracks merge
(`docs/plans/13-register-export-xlsx.md` § The fleet); a track does not edit
this file itself, so two tracks never conflict on it.
"""

from app.main import create_app

# Routes whose `…/export.xlsx` sibling exists. A path listed here without
# the sibling actually served is a defect the first assertion names.
EXPORTED: set[str] = {
    "/api/v1/applications",  # Track A
    "/api/v1/permits",  # Track B
    "/api/v1/oversight/risk-indicators",  # Track D
    "/api/v1/oversight/events",  # Track D
    "/api/v1/admin/public/appeals",  # Track D
    "/api/v1/admin/ratings",  # Track D's remainder, by the coordinator (permits/admin_router.py)
    "/api/v1/norms",  # Track F
    "/api/v1/tariffs",  # Track F
    "/api/v1/rule-parameters",  # Track F
    "/api/v1/gis/contours",  # Track F
    "/api/v1/gis/imports",  # Track F
    "/api/v1/admin/users",  # Track E
    "/api/v1/refs/organizations",  # Track E
    "/api/v1/admin/announcements",  # Track E
    "/api/v1/admin/legal-documents",  # Track E
    "/api/v1/admin/notification-templates",  # Track E
    "/api/v1/admin/integrations/outbox",  # Track E
    "/api/v1/admin/integrations/dead-letters",  # Track E
    "/api/v1/inspections/tasks",  # Track G
    "/api/v1/inspections/acts",  # Track G
    "/api/v1/inspections/cases",  # Track G
    "/api/v1/help/tickets",  # Track G
    "/api/v1/invoices",  # Track C
    "/api/v1/payments/bank-statements",  # Track C
    "/api/v1/payments/reconciliations",  # Track C
    "/api/v1/payments/manual-confirmations",  # Track C
    "/api/v1/payments/allocations",  # Track C
    "/api/v1/refunds",  # Track C
    "/api/v1/payments/recipients",  # Track C
    "/api/v1/search",  # Track H
    "/api/v1/archive",  # Track H
    "/api/v1/reports",  # Track H
    "/api/v1/reports/forms",  # Track H
    "/api/v1/beekeepers",  # Track H
    "/api/v1/certificates",  # Track H
    "/api/v1/signatures",  # Track H
    "/api/v1/notifications",  # Track H
}

# Registers Oybek included («variant a» of question 2) that no track has
# served yet. Remove a path here in the same commit that adds it to EXPORTED.
EXPORT_BACKLOG = {
    # Track F — norms + gis: the two lists no screen reads as a register (a
    # calculation is opened from its application; activity seasons are a
    # calendar) stay here until somebody asks for them
    "/api/v1/calculations",
    "/api/v1/activity-seasons",
}

# No export by design: the public site's lists (anonymous callers, no
# register to keep), and a contour's version history (a sub-list of a card,
# not a register — ruling R8).
EXEMPT = {
    "/api/v1/public/announcements",
    "/api/v1/public/legal-documents",
    "/api/v1/gis/contours/{contour_id}/versions",
}

EXPORT_SUFFIX = "/export.xlsx"


def _paths() -> set[str]:
    return set(create_app().openapi()["paths"])


def _paged_get_paths(paths: set[str]) -> set[str]:
    spec = create_app().openapi()
    out: set[str] = set()
    for path in paths:
        get = spec["paths"][path].get("get")
        if not get:
            continue
        schema = (
            get.get("responses", {})
            .get("200", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        ref = schema.get("$ref", "")
        if ref.rsplit("/", 1)[-1].startswith("Page_"):
            out.add(path)
    return out


def test_the_three_sets_do_not_overlap():
    assert not (EXPORTED & EXPORT_BACKLOG), EXPORTED & EXPORT_BACKLOG
    assert not (EXPORTED & EXEMPT), EXPORTED & EXEMPT
    assert not (EXPORT_BACKLOG & EXEMPT), EXPORT_BACKLOG & EXEMPT


def test_every_exported_register_actually_serves_the_sibling():
    paths = _paths()
    missing = {p for p in EXPORTED if p + EXPORT_SUFFIX not in paths}
    assert not missing, f"listed as exported but no sibling route: {sorted(missing)}"


def test_every_paged_register_is_exported_backlogged_or_exempt():
    paths = _paths()
    paged = _paged_get_paths(paths)
    unaccounted = paged - EXPORTED - EXPORT_BACKLOG - EXEMPT
    assert not unaccounted, (
        "paged GET routes that are neither exported, backlogged nor exempt "
        f"(add them to exactly one set in {__file__}): {sorted(unaccounted)}"
    )


def test_a_served_export_is_not_still_in_the_backlog():
    paths = _paths()
    served = {p[: -len(EXPORT_SUFFIX)] for p in paths if p.endswith(EXPORT_SUFFIX)}
    stale = EXPORT_BACKLOG & served
    assert not stale, f"served now — move from EXPORT_BACKLOG to EXPORTED: {sorted(stale)}"
    gone = (EXPORT_BACKLOG | EXPORTED) - _paged_get_paths(paths)
    assert not gone, f"named here but no longer a paged route: {sorted(gone)}"
