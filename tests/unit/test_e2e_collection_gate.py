"""Collection gate for the live e2e suites (tests/e2e-py), run offline.

Every case collects `tests/e2e-py` in a child pytest (`--collect-only`), so a conftest or import regression in any
live module fails here in the offline suite, naming the module, instead of inside a 45-minute kind-cluster job. Each
case then asserts what a live drive selects: the chaos drills and the soak only when the run asks for them, and the
drills after every other test.
"""

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = "tests/e2e-py"


def _collected(*args: str) -> list[str]:
    """The node ids `pytest --collect-only` selects from the live suites under ``args``, in the order the run would take."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-cov", "-p", "no:cacheprovider", E2E_DIR, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"collecting {E2E_DIR} {' '.join(args)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    return [line for line in proc.stdout.splitlines() if line.startswith(f"{E2E_DIR}/")]


def test_a_plain_live_drive_selects_no_chaos_drill_and_no_soak():
    """A chaos drill takes down the release it runs against, so it runs only where the run asks for it with `--chaos`:
    the e2e-stack lane's throwaway cluster, or an explicit drive of the deployed release (XC-095). The soak reads a drive
    an earlier run kept, so it runs only where the run names that drive with `--soak` (XC-090). A plain drive,
    `scripts/e2e_live.sh` with no flag or `-m phase1`, selects neither."""
    drills = set(_collected("-m", "chaos", "--chaos"))
    soak = set(_collected("-m", "soak", "--soak", "kept-drive.json"))
    plain = set(_collected()) | set(_collected("-m", "phase1"))

    assert drills and soak, "no chaos drill or no soak test is selected even when the run asks for them, so this gate checks nothing"
    assert drills.isdisjoint(plain) and soak.isdisjoint(plain)


def test_a_drive_that_asks_for_the_drills_runs_them_after_every_other_test():
    """A drill takes a dependency of the release down and brings it back, so a test read after one reads an estate just
    through an outage: a run that asks for the drills, the whole live set or Phase 1's acceptance, runs them last."""
    drills = set(_collected("-m", "chaos", "--chaos"))
    every = _collected("--chaos")
    acceptance = _collected("-m", "phase1", "--chaos")

    assert drills, "no chaos drill is selected even when the run asks for them, so this gate checks nothing"
    assert (set(every[-len(drills) :]), set(acceptance[-len(drills) :])) == (drills, drills)
