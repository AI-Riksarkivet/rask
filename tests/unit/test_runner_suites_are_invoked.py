"""The sealed runners' suites must be invoked, and invoked the way that actually works.

`runners/*` is matched by NO workspace glob on purpose — the model stacks must never enter the
fleet's resolution. The cost of that seal is that the root pytest can neither import nor collect
those tests, so each runner suite runs only if the Makefile names it explicitly. Nothing checked
that it did, and two failures had already landed (audit M1):

* **`make test-slow` ran zero slow tests while reading as if it ran them all.** Its first leg is the
  root suite, and the root workspace declares no `slow` mark anywhere — all of them are in
  `runners/htr`. Its second leg invoked that runner FROM THE REPO ROOT (`uv run --project
  runners/htr pytest`), omitting the `cd` that `make test` documents four lines above as mandatory,
  so pytest read the ROOT `testpaths` and died at collection with `ModuleNotFoundError: lineage_kit`
  — a fleet module absent from the runner's venv. Failing on the second line after a two-minute
  green suite reads as "this box has no GPU", not "the target is wrong".
* **`runners/dummy` was named by `make test` but not by `make test-slow`**, so its 10 tests were
  absent from the target that claims to run everything.

This gate is deliberately about the SHAPE of the invocation, not a count: a suite that is invoked
wrongly and a suite that is not invoked at all are the same silent loss, and the `cd` is exactly the
difference between running and dying at collection.

Both directions are NOT checked — a runner may legitimately gain tests before it gains a Makefile
line, and this fails at that moment on purpose. That is the point: adding `runners/<x>/tests`
without wiring it is how a suite comes to run nowhere.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

#: The targets that must cover every sealed runner. `test` is the everyday gate and `test-slow` is
#: the "everything, including the marks the fast lane deselects" gate — a runner missing from either
#: is a suite that does not run in that lane.
TARGETS = ("test", "test-slow")


def _recipe(target: str) -> list[str]:
    """The recipe lines of one make target — tab-indented lines after `<target>:`, comments kept.

    Comments are kept deliberately: they are what a human reads to decide whether a line is missing,
    and stripping them here would let the assertion message point at a body that looks unlike the
    file.
    """
    lines = (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    collecting = False
    for line in lines:
        if re.match(rf"^{re.escape(target)}:", line):
            collecting = True
            continue
        if collecting:
            # A recipe ends at the first line that is neither tab-indented nor blank.
            if line.startswith("\t"):
                out.append(line.strip())
            elif line.strip() == "":
                continue
            else:
                break
    return out


def _sealed_runners_with_tests() -> list[str]:
    return sorted(p.parent.name for p in REPO_ROOT.glob("runners/*/tests") if p.is_dir())


def _sealed_runners() -> list[str]:
    """EVERY sealed runner, with or without a suite — a runner is a directory with its own pyproject."""
    return sorted(p.parent.name for p in REPO_ROOT.glob("runners/*/pyproject.toml"))


#: Sealed runners that ship NO suite, frozen so the roster cannot grow in silence. This is not an
#: aspiration list and the seven names are not a backlog: a runner is sealed model code, and whether it
#: carries tests is that workload's call. What must not happen is a TENTH runner arriving with no tests
#: and nothing saying so — which is exactly what the enumerate-only-runners-that-have-tests discovery
#: below does by construction, since a runner with no `tests/` is invisible to every assertion here.
_RUNNERS_WITHOUT_TESTS = frozenset({"asr", "assist", "diarize", "insid3", "kg", "topics", "voiceprint"})

#: Flags that narrow a pytest run. `-m` is handled separately because one deselection IS sanctioned.
_NARROWING_FLAGS = ("-k", "--deselect", "--ignore", "--last-failed", "--lf", "--stepwise", "--sw")


def test_there_are_sealed_runners_to_check() -> None:
    """Guards the discovery itself — an empty list would make every assertion below vacuous."""
    runners = _sealed_runners_with_tests()
    assert runners, "no runners/*/tests found; the glob is broken, not the estate"


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("runner", _sealed_runners_with_tests())
def test_every_sealed_runner_suite_is_invoked(target: str, runner: str) -> None:
    """`make <target>` must run this runner's suite, from inside the runner's own directory."""
    recipe = _recipe(target)
    assert recipe, f"`{target}:` has no recipe — the Makefile parser or the target moved"

    wired = [line for line in recipe if f"runners/{runner}" in line and "pytest" in line]
    assert wired, (
        f"`make {target}` never runs runners/{runner}'s suite. It is sealed out of the root "
        f"workspace, so the root pytest cannot collect it — unnamed here, it runs NOWHERE.\n"
        f"  {target} recipe:\n    " + "\n    ".join(recipe)
    )

    # The `cd` is the whole difference between running and dying at collection: without it pytest
    # resolves the ROOT `testpaths` and tries to import fleet modules the runner's venv lacks.
    assert all(line.startswith(f"cd runners/{runner}") for line in wired), (
        f"`make {target}` invokes runners/{runner}'s pytest without `cd runners/{runner} &&` "
        f"first. From the repo root pytest reads the ROOT testpaths and exits at COLLECTION, "
        f"which surfaces as an import error rather than as a wiring mistake:\n    " + "\n    ".join(wired)
    )

    # Being NAMED is not the same as being RUN. Until 2026-08-22 the two assertions above were the
    # whole gate, and both are satisfied by a leg narrowed until it selects nothing — `wired` is a
    # substring match and the `cd` check is a `startswith`, so everything after `pytest` was
    # unconstrained. A suite deselected to zero and a suite never invoked are the same silent loss,
    # which is the premise this file opens with.
    for line in wired:
        narrowing = [flag for flag in _NARROWING_FLAGS if flag in line.split()]
        assert not narrowing, (
            f"`make {target}`'s runners/{runner} leg narrows the run with {narrowing}. A selection "
            f"flag here can reduce the suite to zero tests while this gate still passes:\n    {line}"
        )
        marks = re.findall(r'-m\s+"([^"]*)"', line)
        assert all(m.strip() == "not slow" for m in marks), (
            f"`make {target}`'s runners/{runner} leg uses an unsanctioned -m expression {marks}. "
            f"`not slow` is the one deselection this estate means (it is what separates `test` from "
            f"`test-slow`); anything else can empty the run invisibly:\n    {line}"
        )


def test_test_slow_does_not_deselect_the_slow_marks_it_exists_to_run() -> None:
    """The one thing that distinguishes `test-slow` from `test`.

    `make test` deselects `slow`; `make test-slow` must not, or the two targets are the same run
    under two names and the marks are unreachable from either.
    """
    for line in _recipe("test-slow"):
        if "pytest" not in line or line.startswith("#"):
            continue
        assert "not slow" not in line, (
            f"`make test-slow` deselects `slow` — the marks it exists to run become unreachable from every target in the repo:\n    {line}"
        )


@pytest.mark.parametrize("runner", _sealed_runners_with_tests())
def test_an_invoked_runner_suite_is_not_empty(runner: str) -> None:
    """A wired-up leg pointed at a directory holding no tests is a green line that runs nothing.

    Deliberately checks the FILES rather than executing pytest: running the suite for real needs the
    runner's own sealed venv, and the root suite must not depend on one existing — that dependency is
    the whole reason these runners are sealed. So this is a floor, and it is stated as one: it proves
    the directory is not empty, not that every test in it is selected.
    """
    tests_dir = REPO_ROOT / "runners" / runner / "tests"
    functions = [path for path in tests_dir.rglob("test_*.py") if "def test_" in path.read_text(encoding="utf-8")]
    assert functions, (
        f"runners/{runner}/tests is named by the Makefile but contains no `def test_` in any "
        f"`test_*.py`. The leg runs and reports success having collected nothing."
    )


def test_the_runners_shipping_no_suite_are_the_ones_we_know_about() -> None:
    """The silence this file could not break on its own.

    Every assertion above enumerates `_sealed_runners_with_tests()`, so a runner with no `tests/`
    directory is invisible to all of them — seven of the estate's nine are, and the gate reads exactly
    as green as it would if all nine were covered. That is the same shape as the `--continue` finding
    elsewhere in this audit: a report that stops early is indistinguishable from a clean one.

    Freezing the roster does not demand tests from a sealed runner — that is the workload's call, and
    `runners/*` is sealed precisely so those calls stay local. It demands only that the set be stated,
    so a TENTH runner arriving with no suite fails here and someone decides on purpose.
    """
    all_runners = set(_sealed_runners())
    with_tests = set(_sealed_runners_with_tests())
    without = all_runners - with_tests

    assert all_runners, "no runners/*/pyproject.toml found — the glob is broken, not the estate"
    assert all_runners >= _RUNNERS_WITHOUT_TESTS, (
        f"the frozen roster names runners that no longer exist: {sorted(_RUNNERS_WITHOUT_TESTS - all_runners)}. Delete them from _RUNNERS_WITHOUT_TESTS."
    )
    assert without == _RUNNERS_WITHOUT_TESTS, (
        f"the set of sealed runners shipping no suite has changed.\n"
        f"  now without tests: {sorted(without)}\n"
        f"  frozen roster:     {sorted(_RUNNERS_WITHOUT_TESTS)}\n"
        f"  newly untested:    {sorted(without - _RUNNERS_WITHOUT_TESTS)}\n"
        f"  newly tested:      {sorted(_RUNNERS_WITHOUT_TESTS - without)}\n"
        f"A runner that gained a suite should be removed from the roster (and will then be checked "
        f"for a Makefile line by the tests above). A runner that arrived without one is a decision, "
        f"not a default — add it here to record that the decision was made."
    )
