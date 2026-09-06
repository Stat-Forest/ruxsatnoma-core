"""`python -m app.seed`: idempotent upsert of districts and organizations (ruling 5)."""

import json
import sys
import uuid

import pytest
from sqlalchemy import select

from app.core.errors import DomainError
from app.modules.admin.models import District, Organization
from app.seed import load_rows, run, seed_districts, seed_organizations
from app.seed.__main__ import main


def district_rows(suffix: str) -> list[dict]:
    return [
        {
            "code": f"beruniy-{suffix}",
            "soato_code": "1735204",
            "region_code": "karakalpakstan",
            "name": {
                "uz_cyrl": "Беруний тумани",
                "uz_latn": "Beruniy tumani",
                "en": "Beruniy district",
            },
            "sort_order": 10,
        },
        {
            "code": f"nukus-{suffix}",
            "region_code": "karakalpakstan",
            "name": {"uz_cyrl": "Нукус тумани", "uz_latn": "Nukus tumani"},
        },
    ]


def organization_rows(suffix: str, agency_code: str) -> list[dict]:
    """Children of the one agency (ruling 6) — a seed file never invents a second root."""
    return [
        {
            "code": f"territorial-{suffix}",
            "kind": "territorial",
            "parent_code": agency_code,
            "region_code": "karakalpakstan",
            "name": {"uz_cyrl": "Ҳудудий бошқарма", "uz_latn": "Hududiy boshqarma"},
        },
        {
            "code": f"leshoz-{suffix}",
            "kind": "leshoz",
            "parent_code": f"territorial-{suffix}",
            "region_code": "karakalpakstan",
            "name": {"uz_cyrl": "Нукус ДЎХ", "uz_latn": "Nukus DOʻX"},
            "stir": "200388105",
            "requisites": {"account": "40012186035209704220", "mfo": "00014"},
        },
    ]


async def test_districts_created_then_updated(db):
    suffix = uuid.uuid4().hex[:6]
    rows = district_rows(suffix)
    created, updated = await seed_districts(db, rows)
    assert (created, updated) == (2, 0)

    rows[0]["name"] = {"uz_cyrl": "Беруний тумани (янги)", "uz_latn": "Beruniy tumani (yangi)"}
    created, updated = await seed_districts(db, rows)
    assert (created, updated) == (0, 2)

    row = (
        await db.execute(select(District).where(District.code == f"beruniy-{suffix}"))
    ).scalar_one()
    assert row.name["uz_cyrl"] == "Беруний тумани (янги)"
    assert row.soato_code == "1735204"


async def test_unknown_region_code_is_a_clear_error(db):
    rows = district_rows(uuid.uuid4().hex[:6])
    rows[0]["region_code"] = "atlantis"
    with pytest.raises(DomainError) as excinfo:
        await seed_districts(db, rows)
    assert excinfo.value.details is not None
    assert excinfo.value.details["region_code"] == "atlantis"


async def test_null_region_code_is_a_clear_error(db):
    """A `region_code: null` row must raise the same `err(...)` a missing key would —
    not fall through to a bare `AssertionError` (or, under `python -O`, a raw
    NOT NULL `IntegrityError` from Postgres)."""
    rows = district_rows(uuid.uuid4().hex[:6])
    rows[0]["region_code"] = None
    with pytest.raises(DomainError) as excinfo:
        await seed_districts(db, rows)
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "region_code is required"


async def test_organizations_build_the_hierarchy(db, agency):
    suffix = uuid.uuid4().hex[:6]
    created, updated = await seed_organizations(db, organization_rows(suffix, agency.code))
    assert (created, updated) == (2, 0)

    territorial = (
        await db.execute(select(Organization).where(Organization.code == f"territorial-{suffix}"))
    ).scalar_one()
    leshoz = (
        await db.execute(select(Organization).where(Organization.code == f"leshoz-{suffix}"))
    ).scalar_one()
    assert territorial.parent_id == agency.id
    assert leshoz.parent_id == territorial.id
    assert leshoz.requisites["mfo"] == "00014"
    assert leshoz.region_id is not None


async def test_organizations_preserve_region_on_absent_key(db, agency):
    """Re-seeding with `region_code` absent (not null) must not wipe a previously set
    `region_id` — ruling: preserve-on-absence, since a reorganization file describes
    only what changed (finding 2)."""
    suffix = uuid.uuid4().hex[:6]
    rows = organization_rows(suffix, agency.code)
    await seed_organizations(db, rows)

    leshoz_row = dict(rows[1])
    del leshoz_row["region_code"]
    created, updated = await seed_organizations(db, [rows[0], leshoz_row])
    assert (created, updated) == (0, 2)

    leshoz = (
        await db.execute(select(Organization).where(Organization.code == f"leshoz-{suffix}"))
    ).scalar_one()
    assert leshoz.region_id is not None


async def test_organizations_reject_a_wrong_parent_kind(db, agency):
    suffix = uuid.uuid4().hex[:6]
    rows = organization_rows(suffix, agency.code)
    rows[1]["kind"] = "bolak"  # bolak may only hang off aylanma
    with pytest.raises(DomainError):
        await seed_organizations(db, rows)


async def test_organizations_reject_an_archived_parent(db, agency):
    """A parent the write API would refuse (archived) must be refused here too —
    the importer must not build a hierarchy the API would reject (finding 3)."""
    suffix = uuid.uuid4().hex[:6]
    archived = Organization(
        code=f"archived-{suffix}",
        kind="territorial",
        parent_id=agency.id,
        name={"uz_cyrl": "Архив", "uz_latn": "Arxiv"},
        status="archived",
    )
    db.add(archived)
    await db.flush()

    rows = [
        {
            "code": f"leshoz-{suffix}",
            "kind": "leshoz",
            "parent_code": archived.code,
            "name": {"uz_cyrl": "Тест", "uz_latn": "Test"},
        }
    ]
    with pytest.raises(DomainError) as excinfo:
        await seed_organizations(db, rows)
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "parent archived"


async def test_organizations_reject_a_second_root(db, agency):
    """A file describing another agency must fail with a domain error, not an
    IntegrityError from the partial unique index (ruling 6)."""
    with pytest.raises(DomainError) as excinfo:
        await seed_organizations(
            db,
            [
                {
                    "code": "another-agency",
                    "kind": "agency",
                    "name": {"uz_cyrl": "Х", "uz_latn": "X"},
                }
            ],
        )
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "root already exists"


async def test_organizations_reject_a_kind_change(db, agency):
    """The docstring used to claim `kind` "always overwrites", but the update branch
    never reassigned `existing.kind` — so a file changing a row's kind was validated
    against the NEW kind (here: `bolim`'s allowed parent, another leshoz) but stored
    with the OLD one, silently writing a `leshoz` whose parent is itself a `leshoz` —
    a hierarchy the write API would refuse outright (finding, seed CLI drift)."""
    suffix = uuid.uuid4().hex[:6]
    await seed_organizations(
        db,
        [
            {
                "code": f"mover-{suffix}",
                "kind": "leshoz",
                "parent_code": agency.code,
                "name": {"uz_cyrl": "Х", "uz_latn": "X"},
            },
            {
                "code": f"sibling-{suffix}",
                "kind": "leshoz",
                "parent_code": agency.code,
                "name": {"uz_cyrl": "Х", "uz_latn": "X"},
            },
        ],
    )
    with pytest.raises(DomainError) as excinfo:
        await seed_organizations(
            db,
            [
                {
                    "code": f"mover-{suffix}",
                    "kind": "bolim",  # bolim is valid under a leshoz — the sibling
                    "parent_code": f"sibling-{suffix}",
                    "name": {"uz_cyrl": "Х (бўлим)", "uz_latn": "X (boʻlim)"},
                }
            ],
        )
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "kind change is not supported"

    mover = (
        await db.execute(select(Organization).where(Organization.code == f"mover-{suffix}"))
    ).scalar_one()
    assert mover.kind == "leshoz"  # unchanged — the rejected attempt made no write
    assert mover.parent_id == agency.id


async def test_organizations_reject_a_reparent_cycle(db, agency):
    """Mirrors admin.service.update_organization's guard (ruling 6): a re-seed that
    would move a node into its own subtree must be caught the same way, with
    `admin.repo.is_descendant` — a plain FK has no notion of a cycle."""
    suffix = uuid.uuid4().hex[:6]
    await seed_organizations(
        db,
        [
            {
                "code": f"top-{suffix}",
                "kind": "territorial",
                "parent_code": agency.code,
                "name": {"uz_cyrl": "Х", "uz_latn": "X"},
            },
            {
                "code": f"child-{suffix}",
                "kind": "leshoz",
                "parent_code": f"top-{suffix}",
                "name": {"uz_cyrl": "Х", "uz_latn": "X"},
            },
        ],
    )
    with pytest.raises(DomainError) as excinfo:
        await seed_organizations(
            db,
            [
                {
                    "code": f"top-{suffix}",
                    "kind": "territorial",
                    "parent_code": f"child-{suffix}",  # top's own child
                    "name": {"uz_cyrl": "Х", "uz_latn": "X"},
                }
            ],
        )
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "cycle"


async def test_organizations_reject_an_unvalidated_name_shape(db, agency):
    """The write API enforces `LocalizedName` (ruling 13: `uz_cyrl` required, known
    locales only); a seed row bypassing that would store something `/refs` then
    fails to read back."""
    suffix = uuid.uuid4().hex[:6]
    bare_string_name = [
        {
            "code": f"badname-{suffix}",
            "kind": "leshoz",
            "parent_code": agency.code,
            "name": "Нукус",  # not an object at all
        }
    ]
    with pytest.raises(DomainError) as excinfo:
        await seed_organizations(db, bare_string_name)
    assert excinfo.value.details is not None
    assert excinfo.value.details["code"] == f"badname-{suffix}"

    stray_locale_name = [
        {
            "code": f"badname2-{suffix}",
            "kind": "leshoz",
            "parent_code": agency.code,
            "name": {"uz_cyrl": "Номи", "uz_latn": "Nomi", "fr": "Nom"},  # fr is not a known locale
        }
    ]
    with pytest.raises(DomainError):
        await seed_organizations(db, stray_locale_name)


async def test_organizations_reject_non_object_requisites(db, agency):
    suffix = uuid.uuid4().hex[:6]
    rows = [
        {
            "code": f"badreq-{suffix}",
            "kind": "leshoz",
            "parent_code": agency.code,
            "name": {"uz_cyrl": "Х", "uz_latn": "X"},
            "requisites": "not-an-object",
        }
    ]
    with pytest.raises(DomainError) as excinfo:
        await seed_organizations(db, rows)
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "requisites must be an object"


async def test_organizations_reject_a_malformed_stir(db, agency):
    suffix = uuid.uuid4().hex[:6]
    rows = [
        {
            "code": f"badstir-{suffix}",
            "kind": "leshoz",
            "parent_code": agency.code,
            "name": {"uz_cyrl": "Х", "uz_latn": "X"},
            "stir": "not-nine-digits",
        }
    ]
    with pytest.raises(DomainError) as excinfo:
        await seed_organizations(db, rows)
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "invalid stir"


async def test_organizations_reject_a_non_ascii_digit_stir(db, agency):
    """`\\d` is Unicode-aware in Python `re`, unlike the ASCII-only Postgres CHECK —
    nine Arabic-Indic digits must not slip past `_STIR_RE` and reach the DB."""
    suffix = uuid.uuid4().hex[:6]
    rows = [
        {
            "code": f"arabicstir-{suffix}",
            "kind": "leshoz",
            "parent_code": agency.code,
            "name": {"uz_cyrl": "Х", "uz_latn": "X"},
            "stir": "١٢٣٤٥٦٧٨٩",
        }
    ]
    with pytest.raises(DomainError) as excinfo:
        await seed_organizations(db, rows)
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "invalid stir"


async def test_districts_reject_an_unvalidated_name_shape(db):
    rows = district_rows(uuid.uuid4().hex[:6])
    rows[0]["name"] = "Беруний тумани"  # not an object
    with pytest.raises(DomainError) as excinfo:
        await seed_districts(db, rows)
    assert excinfo.value.details is not None


async def test_run_returns_a_summary(db):
    suffix = uuid.uuid4().hex[:6]
    summary = await run("districts", district_rows(suffix), db)
    assert summary == "districts: 2 created, 0 updated"

    with pytest.raises(DomainError):
        await run("planets", [], db)


def test_main_reports_a_domain_error_as_one_line_not_a_traceback(tmp_path, monkeypatch, capsys):
    """Fold-in fix: a `DomainError` (or `KeyError`/`JSONDecodeError`/`FileNotFoundError`)
    must reach the operator as a one-line message and exit(1), not a raw traceback."""
    bad_file = tmp_path / "districts.json"
    bad_file.write_text(
        json.dumps(
            [{"code": "x", "name": {"uz_cyrl": "Х", "uz_latn": "X"}, "region_code": "atlantis"}]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["python -m app.seed", "districts", str(bad_file)])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
    captured_err = capsys.readouterr().err
    assert "Traceback" not in captured_err
    assert "atlantis" in captured_err


def test_main_reports_a_missing_file_as_one_line_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["python -m app.seed", "districts", "/no/such/seed-file.json"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
    captured_err = capsys.readouterr().err
    assert "Traceback" not in captured_err


def test_example_files_parse():
    from pathlib import Path

    base = Path(__file__).resolve().parents[3] / "app" / "seed" / "data"
    districts = load_rows(base / "districts.example.json")
    organizations = load_rows(base / "organizations.example.json")
    assert districts and organizations
    assert {row["kind"] for row in organizations} <= {
        "agency",
        "territorial",
        "leshoz",
        "bolim",
        "aylanma",
        "bolak",
    }
