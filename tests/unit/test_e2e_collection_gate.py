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

import re
import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = "tests/e2e-py"
# The live-suite count, RAISED to the actual number each time a suite lands (22 at wiring time,
# 2026-07; 24 since test_maintenance_s3_e2e.py, #80). A floor left behind the real count is a floor
# with slack in it — the estate had already drifted one suite above 22, so one could have stopped
# collecting and this gate would still have passed. Grows as suites are added; a DROP below it means
# a suite stopped being collected, which is exactly the silent loss this gates.
MIN_SUITE_FILES = 24

# Where pytest is invoked from. Globs rather than a fixed list, so a NEW ci workflow or ops
# script is gated the day it lands instead of the day someone remembers to add it here.
INVOCATION_GLOBS = (
    ".dagger/*.go",
    "scripts/*.sh",
    "Makefile",
    ".github/workflows/*.yml",
    # The suites themselves. Their module docstrings carry run-me instructions, and one carries a
    # functional OpenLineage `_PRODUCER` URI naming its own path — all nine still said `tests/e2e/`
    # after the directory was renamed. A half-applied rename is the same defect as an unapplied one:
    # a human following a stale instruction gets "no tests ran" and concludes the suite is empty.
    "tests/e2e-py/*.py",
    "tests/unit/*.py",
)
# Any repo-relative path under tests/ that names a Python file. Deliberately not anchored to
# the word "pytest": a path may be built into a shell variable or a Go slice several lines
# away from the command that consumes it, and a dead path is misleading wherever it sits.
TEST_PY_PATH = re.compile(r"tests/[A-Za-z0-9_./-]*\.py")


def test_e2e_py_is_wired_into_testpaths():
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths = cfg["tool"]["pytest"]["ini_options"]["testpaths"]
    assert E2E_DIR in testpaths, f"{E2E_DIR} fell out of pytest testpaths — the live e2e gate is silently lost: {testpaths}"


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
    assert len(suite_files) >= MIN_SUITE_FILES, (
        f"only {len(suite_files)} suite files collected from {E2E_DIR} (expected >= {MIN_SUITE_FILES}) — "
        f"a live suite silently dropped out of collection:\n" + "\n".join(sorted(suite_files))
    )


def test_every_invoked_pytest_path_exists():
    """No invocation site may name a test file that does not exist.

    Fails here, in 0.03s offline, rather than as `file or directory not found` partway
    through a live cluster job — and lists every dead path at once instead of only the
    first one `set -e` reaches.
    """
    dead: list[str] = []
    for glob in INVOCATION_GLOBS:
        for site in sorted(REPO_ROOT.glob(glob)):
            for path in sorted(set(TEST_PY_PATH.findall(site.read_text(encoding="utf-8")))):
                if not (REPO_ROOT / path).is_file():
                    dead.append(f"{site.relative_to(REPO_ROOT)} -> {path}")

    assert not dead, (
        "these sites name a test file that does not exist — pytest prints `file or directory not "
        'found` plus "no tests ran" and exits 4, so a caller either dies at its first dead path '
        "having run none of the suites below it, or (in a docstring) sends a human down the same "
        "dead end:\n  " + "\n  ".join(dead)
    )
