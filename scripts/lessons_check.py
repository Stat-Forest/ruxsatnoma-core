#!/usr/bin/env python
"""Structural check for `.claude/lessons.md`.

The file is read before every coding task, so its size is a running cost paid by
every session. Prose cannot hold a size budget on its own — this does.

Fails on a malformed entry (missing one of the three bullets), an entry past the hard
ceiling, the whole file past its cap, or a limit stated differently in prose than it is
here. Entries over the soft budget are reported but do not fail: only a MERGE, which
deletes other entries, earns those lines, and no script can tell a merge from a story.

The per-entry limits and the file cap answer different failures. Short entries are still
entries, so without the cap the file grows back one well-formed lesson at a time — which
is exactly how it reached 1217 lines.

Run: `make lessons-check`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LESSONS = ROOT / ".claude" / "lessons.md"
SKILL = ROOT / ".claude" / "skills" / "writing-lessons" / "SKILL.md"

SOFT_LIMIT = 12  # a new entry
HARD_LIMIT = 24  # a merge replacing two or more entries — nothing goes past this
REQUIRED_BULLETS = ("- **Rule:**", "- **Why:**", "- **How to apply:**")

# The whole-file ratchet. Per-entry limits keep any single entry short but say nothing
# about how MANY there are, and reading cost is the product of the two: the file was
# consolidated at 49 entries / 736 lines, and without a cap it can grow back one
# well-formed entry at a time. The headroom is about one stage's worth of lessons
# (3.7 produced ~10); once it runs out, a new lesson has to be paid for by merging two
# old ones. That is the intended periodic consolidation, not an accident — raise these
# numbers only as a deliberate decision, never to unblock a commit.
#
# Raised from 900 to 1000 lines on 2026-09-06 (Oybek, in chat), as exactly such a
# decision and not to unblock anything. The line cap had done its job and then started
# costing lessons: a session found a real defect, wrote the lesson, and could not record
# it. The consolidation that followed searched all 55 entries of an already-consolidated
# file and found only FIVE honest merges — a much lower yield than the 1217→900 pass,
# which is what a file with little padding left looks like. Meanwhile the codebase went
# from the 13 modules this cap was chosen against to 20, and more modules mean more
# genuinely distinct classes of gotcha rather than more duplicates to squeeze out.
# `MAX_ENTRIES` deliberately stays at 60: at ~15 lines an entry the two caps still bind
# at about the same point, so the ratchet keeps working.
MAX_ENTRIES = 60
MAX_LINES = 1000

# The four numbers above are also stated in prose, in `lessons.md`'s header and in the
# skill — three copies, exactly the "two sources of truth" shape `lessons.md`'s own
# enum-column entry warns about. Nothing but this stopped a raised constant from leaving
# both documents quietly lying. The phrases are BUILT from the constants, so changing one
# fails here, naming the file and the sentence to update. It does pin that wording: reword
# these sentences and the check fails until the pattern below is reworded with them. That
# is the trade — a fixed phrase per number, against prose that silently goes stale.
DECLARED_IN_PROSE = {
    LESSONS: (
        f"at most {SOFT_LIMIT} lines",
        f"never past {HARD_LIMIT}",
        f"{MAX_ENTRIES} entries / {MAX_LINES} lines",
    ),
    SKILL: (
        f"{SOFT_LIMIT} lines is the budget",
        f"{HARD_LIMIT}-line ceiling",
        f"{MAX_ENTRIES} entries / {MAX_LINES} lines",
    ),
}


class Entry:
    def __init__(self, title: str, line_no: int) -> None:
        self.title = title
        self.line_no = line_no
        self.body: list[str] = []

    @property
    def length(self) -> int:
        """Non-blank body lines. Blank lines are free — they aid reading."""
        return sum(1 for line in self.body if line.strip())

    def missing_bullets(self) -> list[str]:
        text = "\n".join(self.body)
        return [b for b in REQUIRED_BULLETS if b not in text]


def parse(path: Path) -> list[Entry]:
    """Collect `## ` entries, ignoring anything inside a fenced code block.

    The file's own header carries a `## {Short topic title}` template inside a
    fence; without the fence check it would parse as an entry and fail every run.
    """
    entries: list[Entry] = []
    current: Entry | None = None
    in_fence = False

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        if line.startswith("## "):
            current = Entry(line[3:].strip(), line_no)
            entries.append(current)
        elif line.startswith("# ") or line.rstrip() == "---":
            current = None  # a section heading or rule ends the previous entry
        elif current is not None:
            current.body.append(line)

    return entries


def prose_drift() -> list[str]:
    """Report every limit a document states differently from the constant above it."""
    problems: list[str] = []
    for path, phrases in DECLARED_IN_PROSE.items():
        if not path.exists():
            problems.append(
                f"  {path.relative_to(ROOT)} not found — it states the limits and must exist"
            )
            continue
        text = path.read_text(encoding="utf-8")
        for phrase in phrases:
            if phrase not in text:
                problems.append(
                    f'  {path.name} no longer says "{phrase}"\n'
                    f"      A limit changed in {Path(__file__).name} and the prose still\n"
                    f"      carries the old number, or the sentence was reworded. Update the\n"
                    f"      document, or DECLARED_IN_PROSE if the wording moved on purpose."
                )
    return problems


def main() -> int:
    if not LESSONS.exists():
        print(f"FAIL: {LESSONS} not found")
        return 1

    entries = parse(LESSONS)
    total_lines = len(LESSONS.read_text(encoding="utf-8").splitlines())
    failures: list[str] = prose_drift()
    over_soft: list[Entry] = []

    for entry in entries:
        missing = entry.missing_bullets()
        if missing:
            failures.append(
                f"  {LESSONS.name}:{entry.line_no}  missing {', '.join(missing)}\n"
                f"      {entry.title}"
            )
        if entry.length > HARD_LIMIT:
            failures.append(
                f"  {LESSONS.name}:{entry.line_no}  {entry.length} lines, ceiling is {HARD_LIMIT}\n"
                f"      {entry.title}"
            )
        elif entry.length > SOFT_LIMIT:
            over_soft.append(entry)

    print(f"lessons.md: {len(entries)}/{MAX_ENTRIES} entries, {total_lines}/{MAX_LINES} lines")

    if len(entries) > MAX_ENTRIES or total_lines > MAX_LINES:
        what = []
        if len(entries) > MAX_ENTRIES:
            what.append(f"{len(entries)} entries, cap is {MAX_ENTRIES}")
        if total_lines > MAX_LINES:
            what.append(f"{total_lines} lines, cap is {MAX_LINES}")
        fattest = sorted(entries, key=lambda e: -e.length)[:3]
        failures.append(
            f"  the file is over its cap ({'; '.join(what)}).\n"
            f"      A new lesson now costs an old one: merge two entries of the same\n"
            f"      class, or delete one whose trap a check has since made impossible.\n"
            f"      Largest entries, as merge candidates:\n"
            + "\n".join(f"        {e.length:>3}  {e.title}" for e in fattest)
        )

    if over_soft:
        print(f"\n  over the {SOFT_LIMIT}-line budget (fine only if it MERGED other entries):")
        for entry in sorted(over_soft, key=lambda e: -e.length):
            print(f"    {entry.length:>3}  {entry.title}")

    if failures:
        print("\nFAIL:")
        print("\n".join(failures))
        return 1

    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
