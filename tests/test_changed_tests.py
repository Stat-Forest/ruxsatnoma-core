"""`scripts/changed_tests.py` — the picker behind `make check`'s test step
(decision #228). A picker that selects too little is the dangerous direction:
the gate goes green having skipped the tests that would have failed, and only
CI notices. So every rule is pinned, and every unknown path must widen to ALL."""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "changed_tests", Path(__file__).resolve().parent.parent / "scripts" / "changed_tests.py"
)
assert _SPEC and _SPEC.loader
changed_tests = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(changed_tests)
select = changed_tests.select
ALWAYS = set(changed_tests.ALWAYS)


def _paths(result: str) -> set[str]:
    return set(result.split())


def test_a_change_in_a_module_runs_that_modules_tests_and_the_static_guards() -> None:
    assert (
        _paths(select(["app/modules/payments/service.py"])) == {"tests/modules/payments"} | ALWAYS
    )


def test_docs_lessons_and_ci_run_nothing() -> None:
    assert select(["CLAUDE.md", ".claude/lessons.md", ".github/workflows/ci.yml"]) == ""


def test_shared_code_and_unknown_paths_run_everything() -> None:
    for path in (
        "app/core/errors.py",
        "app/main.py",
        "app/event_subscriptions.py",
        "migrations/versions/0001_x.py",
        "tests/conftest.py",
        "pyproject.toml",
        "uv.lock",
        "somewhere/new.txt",
    ):
        assert select([path]) == "ALL", path


def test_a_changed_test_helper_runs_every_test_that_imports_it() -> None:
    """`make_user` lives in a test module of `auth` and is imported across the
    suite — the import graph, not the directory, decides what runs."""
    picked = _paths(select(["tests/modules/auth/test_sessions.py"]))
    assert "tests/modules/auth/test_sessions.py" in picked
    assert any(p.startswith("tests/modules/payments") for p in picked)


def test_a_changed_package_conftest_runs_the_package_and_its_importers() -> None:
    picked = _paths(select(["tests/modules/gis/conftest.py"]))
    assert "tests/modules/gis" in picked
    assert "tests/modules/permits" in picked  # permits' conftest imports gis's


def test_a_deleted_test_side_file_runs_everything() -> None:
    assert select(["tests/modules/payments/test_no_such_file.py"]) == "ALL"
