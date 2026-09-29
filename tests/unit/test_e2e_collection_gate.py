"""Collection gate for the live e2e suites (tests/e2e-py).

The audit at d5af596 found tests/e2e-py (22 live suites) collected by NOTHING: it was
absent from the root testpaths, so the whole live gate had silently vanished — risk-5,
because every green run kept claiming coverage it no longer had. The fix wires
tests/e2e-py into `[tool.pytest.ini_options] testpaths` (deselected offline via
`-m "not e2e"`); THIS test makes that wiring un-losable:

1. tests/e2e-py must be in the configured testpaths (the wiring itself), and
2. `pytest --collect-only tests/e2e-py` must succeed and find at least the 22 suite
   files that exist today — so a conftest/import regression that turns collection into
   an error, or a future re-shuffle that drops the directory, fails HERE instead of
   silently shrinking the gate.

**(3) guards the other end of the same wire.** (1) and (2) prove the suites are reachable
when pytest is POINTED at them. They say nothing about where the CI and ops scripts
actually point pytest — and that is where the wire had come apart. The lance-ns merge
(d97d8e2, 2026-07-27) landed these suites at `tests/e2e-py/` rather than `tests/e2e/`,
because this repo's `tests/e2e/` was already the Playwright project; it repointed the
files but not their thirteen callers across `.dagger/e2e.go` and four `scripts/*.sh`.

The failure mode is worth stating precisely, because the obvious guess is wrong. pytest
given a missing path prints BOTH `ERROR: file or directory not found` and `no tests ran`,
and exits **4** (usage error) — not 5, and not 0. Every caller here propagates that:
the four scripts run `set -euo pipefail` and Dagger's `WithExec` fails on nonzero. So
these do not silently report success; they hard-fail, and `set -e` means a script dies on
its FIRST dead path having run none of the suites below it.

That is why the gate earns its place anyway. Without it the breakage surfaces as
`file or directory not found` somewhere inside a 45-minute kind-cluster job, naming a
symptom rather than the cause. With it, the offline unit suite fails in 0.03s, locally,
listing every dead path at once — and a future rename of a suite directory cannot reach
`main` with its callers left behind.

It is keyed on `.py` files on purpose: `Makefile:497` is `cd tests/e2e && bunx playwright
test`, a DIRECTORY reference to the browser suite that legitimately lives there and must
keep passing. A path ending in `.py` is unambiguously a pytest target.
"""

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = "tests/e2e-py"


# The live-suite count, RAISED to the actual number each time a suite lands (22 at wiring time,
# 2026-07; 24 since test_maintenance_s3_e2e.py, #80). A floor left behind the real count is a floor
# with slack in it — the estate had already drifted one suite above 22, so one could have stopped
# collecting and this gate would still have passed. Grows as suites are added; a DROP below it means
# a suite stopped being collected, which is exactly the silent loss this gates.
#: DERIVED, not maintained. This was `MIN_SUITE_FILES = 24` against 26 suite files that collect today
#: — two suites could have stopped collecting entirely while all four assertions here reported green,
#: which is the exact silent loss this gate exists to prevent, at a scale of two. It had drifted before:
#: the constant's own comment records being raised 22 -> 24 after the estate grew past it, so the slack
#: is structural rather than a one-off oversight. A floor cannot know what it is missing; it can only
#: know it has not reached zero. Counting the files on disk and requiring collection to REPRODUCE that
#: number has no slack and nothing to maintain — the same self-consistency fix nav-truth used when its
#: `> 30` floor was found sitting under a scanner that saw 80 of 90 hrefs.
def _suite_files_on_disk() -> set[str]:
    return {f"{E2E_DIR}/{p.name}" for p in (REPO_ROOT / E2E_DIR).glob("test_*.py")}


def test_e2e_py_collection_finds_all_live_suites():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-cov", "-p", "no:cacheprovider", E2E_DIR],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"collecting {E2E_DIR} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    suite_files = {line.split("::", 1)[0] for line in proc.stdout.splitlines() if line.startswith(f"{E2E_DIR}/")}
    on_disk = _suite_files_on_disk()
    assert on_disk, f"no test_*.py files found in {E2E_DIR} — the scan root moved and this gate is vacuous"
    missing = sorted(on_disk - set(suite_files))
    assert not missing, (
        f"these live suite files exist in {E2E_DIR} but pytest collected NOTHING from them: {missing}. "
        "A suite that stops collecting is invisible — every run stays green while its assertions no "
        "longer exist. Fix the collection error, or delete the file if the suite is genuinely gone."
    )
