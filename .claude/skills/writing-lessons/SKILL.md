---
name: writing-lessons
description: Use when finishing a ruxsatnoma backend task that fixed a non-trivial bug or turned up a non-obvious gotcha, when a review finding needs recording, or before appending anything to .claude/lessons.md
---

# Writing Lessons

## Overview

`.claude/lessons.md` is read before every coding task in this repo, so every line is a
cost paid by every future session. It grew to 1217 lines / 66 entries because "APPEND a
lesson" was the only instruction anyone had: no step asked whether the trap was already
written down, whether it could be a check instead, or how long an entry may be.

**A lesson is what you write when you cannot automate the check.** That ordering is the
whole skill.

## Your output is exactly one of four things

Decide which BEFORE writing prose. Most findings are not D.

| | Output | When |
|---|---|---|
| **A** | A mechanical check — a test, a `make` target, a ruff rule — and NO entry | The trap is greppable or assertable |
| **B** | An EDIT to an existing entry, adding your case as a sub-bullet | An existing entry's Rule covers the class |
| **C** | A line in another file, no entry here | It is a convention, a ruling, or a question |
| **D** | A NEW entry: heading + three bullets, ≤12 lines | None of the above |

## Step 1 — Can it be a check instead? (A)

If a grep or an assert would have caught your bug, write that instead of a paragraph.
A check runs every time; a paragraph runs only when someone reads it.

- greppable pattern → a test or a CI grep (`date.today()`, `:\w+::` in `sa.text`)
- an invariant between two files → a test asserting they agree
  (`test_the_schema_literals_match_the_tables_own_check_constraints` is the shape)
- a schema/tooling fact → `make check` / pre-commit, never a pytest test that expects to
  report it (see the conftest-autouse entry — the fixture raises first)

Write the check, and then either write no entry at all, or one line pointing at it —
`make heads` is how the multiple-Alembic-heads entry earns its place.

## Step 2 — Does the class already exist? (B)

Run both, always:

```bash
grep '^## ' .claude/lessons.md
grep -in '<the symbol, error, or function from your bug>' .claude/lessons.md
```

**Observable predicate:** if any existing entry's **Rule** line, followed literally,
would have prevented your bug — that is your entry. Sharpen it. Add your case as a
sub-bullet under it (`- **The mirror, in 3.7 t4:** …`) and widen the title if the class
is now broader than it says.

Your incident will feel unique; the class rarely is. "Different module", "different
permission", "different table" are not different classes.

## Step 3 — Is it even a lesson? (C)

| The finding is | Goes to |
|---|---|
| A rule for writing code here, obvious once stated | `CLAUDE.md` |
| Something only an operator does at deploy time | `CLAUDE.md` → Deploy notes |
| A product or design ruling | `../docs/decisions.md`, only after Oybek's explicit OK |
| A question for the customer | `../docs/tz/12-otkrytye-voprosy.md` |
| What is done and what is next | `../docs/status.md` |

A lesson is what is NOT obvious from reading the code. "Use async" is a convention;
"`\d` means something different in Postgres" is a lesson.

## Step 4 — Write the entry (D)

```markdown
## {The rule, stated as a title — not the symptom}

- **Rule:** {1–2 lines: what to do, in the imperative}
- **Why:** {2–3 lines: the concrete symptom — the error text, the status code, the file
  — plus the stage or commit. Evidence, not a story.}
- **How to apply:** {1–2 lines: the trigger that fires this, and where the reference
  implementation lives}
```

Place it under the section it belongs to. **12 lines is the budget.** Only a merge —
an entry that replaces two or more existing ones — may go past it, never past 24.

What "evidence, not a story" means, on the same finding:

> **Story (28 lines):** …confirmed empirically by hard-coding `used_sb=None` to a real
> Decimal and watching the brief-verbatim test stay green throughout, which proved it has
> no assertion that could ever fail from that change, and then reverting…
>
> **Evidence (2 lines):** …hard-coding `run_checks`'s `used_sb` to a real Decimal left the
> brief-verbatim test green — the HTTP scenario never calls the function.

How you found it is not reusable. What fires it, and what it broke, are.

## Step 5 — Verify

```bash
make lessons-check
```

Fails on a missing bullet or an entry past the ceiling; lists anything over budget.
Being on that list is fine only if you deleted entries to earn it. The same check runs
as a pre-commit hook, inside `make check`, and in CI's `lint` job — so a malformed entry
stops your commit and then the PR. Catch it here instead.

## Common mistakes

| Mistake | Fix |
|---|---|
| Appending because the task said "append a lesson" | Steps 1–3 first; A/B/C are the common answers |
| A title naming the symptom (`ERR-GIS-003 returns 500`) | Title the rule (`Coerce before every JSON boundary`) |
| Restating the whole review thread | Three bullets. The thread is in the PR |
| A second entry for a class that exists | Sub-bullet under the existing entry |
| Writing a paragraph for something grep would catch | Step 1 — write the check |
| No trigger, so nobody knows when it fires | "How to apply" names a concrete moment |
