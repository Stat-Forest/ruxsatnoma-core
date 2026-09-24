# beekeepers

Stage 10, rulings #181/#182 (`docs/decisions.md`). The Beekeeping Union's own
register of certificate holders, kept by `beekeeping_registrar` (renamed from
`benefit_verifier` by migration `0053`, same role id) — and the seam
`applications` calls to check the `beekeeping_union_member` benefit claim
against it without a human in the loop (ruling #219).

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
"not_yours", "expired"]`, `beekeeper_id: uuid.UUID | None`. Pure function of
this register only — no application knowledge, no exceptions for business
outcomes. `active` rows only (a `removed` row reads as `unknown`, never as a
stale match); the certificate number is trimmed and case-folded before
comparison; identity is PINFL when given, else STIR (ruling #182 option а —
the caller passes the applicant's own identity, so a legal entity is matched by
its STIR); `expired` is a row the identity owns whose `valid_to` (ruling #217)
is before the business day.

**Called on every step that can reach a signature — ruling #219
(2026-09-24).** `applications.service._check_benefit_claim` asks it at the
pre-check (the wizard's "Next" on step 4), at the package and at filing:
`unknown`/`not_yours`/`expired` refuse 422 `ERR-APP-003`
(`benefit_certificate_unknown`/`_not_yours`/`_expired`), `matched` verifies the
claim on the spot (`benefit_verified_by` NULL — "the register, not a human").
#219 supersedes #206 (2026-09-13), which had switched the check off and sent
every claim to the leshoz; `applications` imports this service directly
(level 3 -> level 2), the seam `BENEFIT_AUTO_VERIFIERS` that stage 10 used is
not coming back. The one step that does NOT ask is `GET /applications/{id}/
package`: the head signs the decision over those bytes, and a register change
after filing must not stop a decision.

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
