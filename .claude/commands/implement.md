---
description: Autonomously implement a task from the project plan (research → code → tests → PR → review loop)
argument-hint: <task number from plans/, e.g. 1.6>
---

# Autonomous task implementation

You are implementing a task of the Ruxsatnoma project plan in
`Stat-Forest/ruxsatnoma-core`. Work autonomously — research, build, test, open
the PR, run the review loop — until the PR is ready to merge.

**Stack:** Python 3.13 · FastAPI · SQLAlchemy 2.0 async + GeoAlchemy2 ·
PostgreSQL 18 + PostGIS · Alembic · Pydantic v2 · structlog · aio-pika · Redis.
Everything that talks to an external system belongs to
`ruxsatnoma-integration`, never here.

## Task: $ARGUMENTS

---

## Step 0: Load context (MANDATORY — do this FIRST)

Read, in this order, before writing any code:

1. `CLAUDE.md` — repository rules and where the documentation lives
2. `.claude/lessons.md` — if a lesson matches the area of work, it OVERRIDES
   your instinct
3. The task itself in `../ruxsatnoma-docs/plans/` — find the phase file the
   number belongs to and read the task end to end, including every step
4. The documents that task says it consumes — typically
   `engineering-standards.md`, `modules.md`, `database.md` or `contracts.md`

## Step 0.5: Branch

```bash
git status --porcelain    # not empty? STOP and ask. Never clobber work.
git fetch origin main
git checkout -b feat/<task number>-<short-slug> origin/main
```

## Step 1: Research

Skip for trivial fixes. Otherwise find two or three existing files of the same
kind and read them end to end before writing a new one. Mirror their structure.
The repository is young — where there is no precedent, follow `modules.md`
exactly.

## Step 2: Implement

Invariants that are not negotiable:

- **Money** is `numeric(18,2)` and `Decimal`. Never `float`, never `int()`
  truncation — the legacy system dropped tiyin that way.
- **Time** is `timestamptz`, stored in UTC. Naive datetimes are rejected by
  ruff rule `DTZ`.
- **English only** in code, comments, docstrings, commit messages and branch
  names. User-facing text goes to `locales/`.
- **Package boundaries:** a module touches only its own schema, even for reads.
  Cross-module access goes through the package's public API.
- **One transaction changes one aggregate.** The single documented exception is
  the contour-occupancy check together with application creation.

## Step 3: Test

Follow the pyramid in the engineering standards §5. Formulas and the status
machine are TDD — failing test first. Integration tests use testcontainers with
PostGIS; SQLite is never used.

## Step 4: Gate

```bash
make check
```

Every step must pass. A red gate is never "flaky" — read the output.

## Step 5: Pull request

Check whether this branch's PR is still alive BEFORE every push — a merged or
closed PR does not pick up new commits, and the work silently never reaches
`main`:

```bash
gh pr list --head "$(git branch --show-current)" --state all --json number,state,mergedAt
```

Open → push, it updates itself. Merged or closed → leave the branch alone, cut
a fresh one from `origin/main`, move the commits, open a new PR. None → push and
create one:

```bash
git push -u origin HEAD
gh pr create --base main --title "<conventional commit title>" --body "<what and why, in Russian; reviewers are people>"
```

No AI attribution in the commit trailer or the PR body.

## Step 6: Review loop

Follow the bugbot review cadence from the tech lead's global instructions
(`~/.claude/CLAUDE.md`): poll every five minutes, escalate with one
`@bugbot review` only after thirty minutes of silence. **This command requires a
streak of five consecutive clean rounds** — that number is set here, the cadence
is set there. Any new finding resets the counter to zero.

## Step 7: Record what you learned

If the task surfaced a non-obvious gotcha, append a lesson to
`.claude/lessons.md` in the documented format before calling the task done.
