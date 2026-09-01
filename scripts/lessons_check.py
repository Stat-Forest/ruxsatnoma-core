#!/usr/bin/env python
"""Structural check for `.claude/lessons.md`.

The file is read before every coding task, so its size is a running cost paid by
every session. Prose cannot hold a size budget on its own — this does.

Fails on a malformed entry (missing one of the three bullets), an entry past the hard
ceiling, or the whole file past its cap. Entries over the soft budget are reported but
do not fail: only a MERGE, which deletes other entries, earns those lines, and no script
can tell a merge from a story.

The per-entry limits and the file cap answer different failures. Short entries are still
entries, so without the cap the file grows back one well-formed lesson at a time — which
is exactly how it reached 1217 lines.

Run: `make lessons-check`.
"""

from __future__ import annotations

import sys
from pathlib import Path

LESSONS = Path(__file__).resolve().parent.parent / ".claude" / "lessons.md"

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
MAX_ENTRIES = 60
MAX_LINES = 900


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


def main() -> int:
    if not LESSONS.exists():
        print(f"FAIL: {LESSONS} not found")
        return 1

    entries = parse(LESSONS)
    total_lines = len(LESSONS.read_text(encoding="utf-8").splitlines())
    failures: list[str] = []
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
