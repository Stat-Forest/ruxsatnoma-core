"""Print the pytest paths `make check` runs: the tests of what changed since `dev`.

`make check` used to run the whole suite before every commit, and this machine
runs several sessions at once — each commit cost ~6 minutes of the one
PostgreSQL. CI still runs everything on every push and nothing merges before it
is green (decision #228), so the local gate only has to cover what this branch
touched. Output, one line:

  - nothing — only docs, lessons, CI or deploy files changed;
  - `ALL` — something shared changed (`app/core`, the root conftest, a
    migration, a dependency) or a path no rule knows: when in doubt, run MORE;
  - otherwise the test paths, space-separated.

What counts as changed: everything differing from the merge-base with
`origin/dev` (committed or not) plus untracked files.

Rules, in order:
  - `app/modules/<m>/...`   -> `tests/modules/<m>`
  - a test-side file under `tests/` -> that file if it is a test module, its
    whole package if it is a `conftest.py`, plus every test file that imports
    it, transitively — `make_user` in `tests/modules/auth/test_sessions.py` is
    imported by a hundred files in other modules;
  - the always-run static guards (`ALWAYS`) join any non-empty selection:
    they read the whole app, are cheap, and catch the cross-cutting rules (an
    unbounded request field, a route without a permission) that a module's own
    tests never would.
"""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# Changes that need no test run at all.
NO_TESTS_PREFIXES = (".claude/", ".github/", "deploy/", "docker/", "docs/")
NO_TESTS_SUFFIXES = (".md",)
NO_TESTS_FILES = {
    "docker-compose.yml",
    "docker-compose.deploy.yml",
    ".gitignore",
    ".pre-commit-config.yaml",
    "scripts/lessons_check.py",
}

# Static guards over the whole app — see the module docstring.
ALWAYS = (
    "tests/test_code_conventions.py",
    "tests/test_export_routes.py",
    "tests/test_permissions_registry.py",
    "tests/test_request_bounds.py",
    "tests/test_request_stripping.py",
)


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True)
    return [line for line in out.stdout.splitlines() if line]


def changed_files() -> list[str]:
    base = _git("merge-base", "HEAD", "origin/dev")[0]
    return sorted(
        set(_git("diff", "--name-only", base))
        | set(_git("ls-files", "--others", "--exclude-standard"))
    )


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _test_import_graph() -> dict[str, set[Path]]:
    """`tests.x.y` -> the test-side files that import it (or a name from it)."""
    importers: dict[str, set[Path]] = {}
    for path in TESTS.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tests"):
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names if a.name.startswith("tests")]
            for name in names:
                importers.setdefault(name, set()).add(path)
    return importers


def _with_importers(seeds: set[Path]) -> set[Path]:
    importers = _test_import_graph()
    seen, stack = set(seeds), list(seeds)
    while stack:
        for importer in importers.get(_module_name(stack.pop()), ()):
            if importer not in seen:
                seen.add(importer)
                stack.append(importer)
    return seen


def select(files: list[str]) -> str:
    targets: set[str] = set()
    test_seeds: set[Path] = set()
    for f in files:
        if f in NO_TESTS_FILES or f.startswith(NO_TESTS_PREFIXES) or f.endswith(NO_TESTS_SUFFIXES):
            continue
        parts = f.split("/")
        if parts[:2] == ["app", "modules"] and len(parts) > 3:
            targets.add(f"tests/modules/{parts[2]}")
        elif parts[0] == "tests" and f.endswith(".py") and f != "tests/conftest.py":
            if (ROOT / f).exists():
                test_seeds.add(ROOT / f)
            else:  # a deleted test-side file: its importers are already edited or broken
                return "ALL"
        else:
            return "ALL"

    for path in _with_importers(test_seeds):
        if path.name == "conftest.py":
            targets.add(str(path.parent.relative_to(ROOT)))
        elif path.name.startswith("test_"):
            targets.add(str(path.relative_to(ROOT)))
        # a helper module that is not a test itself is covered by its importers

    if not targets:
        return ""
    # A file inside a selected package is already run by the package.
    packages = {t for t in targets if not t.endswith(".py")}
    kept = {t for t in targets if not any(t.startswith(p + "/") for p in packages)}
    return " ".join(sorted(kept | set(ALWAYS)))


if __name__ == "__main__":
    print(select(sys.argv[1:] or changed_files()))
