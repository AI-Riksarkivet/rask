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
"""

import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = "tests/e2e-py"
# The live-suite count at wiring time (2026-07). Grows as suites are added; a DROP below
# this floor means a suite stopped being collected — exactly the silent loss this gates.
MIN_SUITE_FILES = 22


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
