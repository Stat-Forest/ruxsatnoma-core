# beekeepers

Stage 10, rulings #181/#182 (`docs/decisions.md`). The Beekeeping Union's own
register of certificate holders, kept by `beekeeping_registrar` (renamed from
`benefit_verifier` by migration `0053`, same role id) — and the seam
`applications` calls to auto-verify the `beekeeping_union_member` benefit
claim without a human in the loop.

Level 2 (design/01): a self-contained "tool", the same shelf as `signatures`/
`gis`/`norms`/`notifications`. It reaches `auth` (level 1) for one read —
`auth.service.get_oneid_snapshot_by_pinfl` — and nothing else below it, and
calls no sibling level-2 module. `applications` (level 3) is the one caller
above it.

## Routes (`app/modules/beekeepers/router.py`)

Every route requires `beekeepers.manage`. There is no `DELETE` route — a
member is `removed`, never erased.

| Method | Path | |
|---|---|---|
| `GET` | `/beekeepers` | paged list; `q` (substring over certificate_no/full_name/pinfl), `status` |
| `GET` | `/beekeepers/lookup?pinfl=` | the "honest auto-fill" (ruling #182): name + passport from a OneID profile already on file for this PINFL, 404 `ERR-SYS-003` otherwise |
| `POST` | `/beekeepers` | 201 |
| `PATCH` | `/beekeepers/{id}` | partial update |
| `POST` | `/beekeepers/{id}/remove` | soft-remove; `reason` is mandatory |

## The seam (`service.match_certificate`)

```python
async def match_certificate(
    db: AsyncSession, *, certificate_no: str, pinfl: str | None, stir: str | None
) -> MatchResult
```

`MatchResult` is a frozen dataclass: `status: Literal["matched", "unknown",
"not_yours"]`, `beekeeper_id: uuid.UUID | None`. Pure function of this
register only — no application knowledge, no exceptions for business
outcomes. `active` rows only (a `removed` row reads as `unknown`, never as a
stale match); the certificate number is trimmed and case-folded before
comparison; identity is PINFL when given, else STIR (ruling #182 option а —
the caller picks which one to pass by the application's `on_behalf`).

**Nobody calls this at filing any more — ruling #206 (2026-09-13).** Stage 10
wired it into `applications.BENEFIT_AUTO_VERIFIERS` so that an unknown or
someone else's number refused the submission (`benefit_certificate_unknown` /
`_not_yours`) and a match verified the claim on the spot; #206 removed both
directions — every numbered claim opens `pending` for the leshoz, and the
seam is deleted rather than left empty. The function stays as the register's
own query (its tests live in `tests/modules/beekeepers/`), ready for a
reviewer-side lookup on the application card if one is ever asked for.

## Data (migration `0053`)

`beekeepers`: `certificate_no`, `pinfl`, `passport_series`, `passport_number`,
`stir` (nullable — a legal-entity member), `full_name`, `farm_name`
(nullable), `status` (`active`/`removed`), `removed_reason` (mandatory when
removed), `created_by`/`updated_by`/`created_at`/`updated_at`. Rows are never
deleted; `uq_beekeepers_certificate_no_active` is a PARTIAL unique index
(`status='active'` only) so a removed member's number can be re-registered.

The same migration renames the role, moves `benefits.verify` off it onto
`executor_staff`/`executor_head`, grants it the new `beekeepers.manage`
permission, and seeds the seven `benefit_categories` classifier items of
ruling #181.
