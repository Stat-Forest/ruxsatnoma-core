#!/usr/bin/env python3
"""Fail if non-Latin characters appear anywhere in the source tree.

User-facing text belongs in locale files, never in the source. See the
engineering standards, section 7.
"""

import re
import sys
from pathlib import Path

# Cyrillic ranges, written as escape sequences so that this file passes its
# own check: U+0400-U+04FF Cyrillic, U+0500-U+052F Cyrillic Supplement.
NON_LATIN = re.compile(r"[\u0400-\u04ff\u0500-\u052f]")
EXCLUDED = ("locales/", "tests/fixtures/")


def main(paths: list[str]) -> int:
    failures: list[str] = []
    for raw in paths:
        path = Path(raw)
        if any(part in str(path) for part in EXCLUDED):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if NON_LATIN.search(line):
                failures.append(f"{path}:{number}: {line.strip()[:80]}")
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        print(
            f"\n{len(failures)} line(s) contain non-Latin characters. "
            "Move user-facing text to locales/.",
            file=sys.stderr,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
