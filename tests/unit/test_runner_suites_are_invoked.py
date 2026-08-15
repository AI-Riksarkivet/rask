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
