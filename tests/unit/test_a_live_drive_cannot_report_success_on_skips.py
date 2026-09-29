"""A target that drives the live estate must not exit 0 having skipped its way past the proof.

[[LH-196]]. Every suite under `tests/e2e-py` guards each test on the environment it needs, so a
missing port-forward or an unseeded token turns a proof into a skip — and pytest reports skips as
success. `make e2e-dummy-lane` is the case that was measured: it guards `LANCE_E2E_CATALOG_URL` and
forwards only that, while the suite also reads `LANCE_E2E_ADMIN_TOKEN` and `LANCE_E2E_LINEAGE_URL`, so
an operator with one variable set gets `3 passed, 4 skipped` and a zero exit from the estate's only
GPU-free cascade prover.

TWO HALVES, AND THE FIRST IS NOT ENOUGH ON ITS OWN. Guarding the variables makes the failure early and
names what is missing — the pattern `e2e-governed-union` and `e2e-medallion` already follow — but it
cannot reach the skips that are not about a variable at all: a token that is not a project admin, an
estate with no Ray head. `--require-live` covers those, because it asks the only question that matters
to a live drive: did every test it collected actually run?

THE MECHANISM IS EXERCISED RATHER THAN READ. A source assertion that the flag appears in the Makefile
proves the flag is typed, not that it fails a run — the shape this file exists to end. So the first
test drives pytest for real, on a throwaway suite, and asserts the exit code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_E2E = _ROOT / "tests" / "e2e-py"


def _drive(tmp_path: Path, body: str, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run pytest on a standalone file, loading the REAL plugin module the e2e conftest re-exports.

    Imported rather than reimplemented: a probe that defined its own hooks would pass while the estate
    had none. Not the whole conftest, because that file also carries a session-scoped cleanup fixture
    that reaches for a live estate — standing that up to test a flag is the wrong trade, and the
    conftest's own `__all__` is what keeps the two in step.
    """
    (tmp_path / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(_E2E)!r})\nfrom require_live import pytest_addoption, pytest_sessionfinish\n",
        encoding="utf-8",
    )
    (tmp_path / "test_probe.py").write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(tmp_path / "test_probe.py"), *flags],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )


_SKIPS = """
import pytest


def test_it_runs() -> None:
    assert True


def test_it_skips() -> None:
    pytest.skip("no port-forward")
"""


def test_a_run_that_SKIPPED_fails_under_require_live(tmp_path: Path) -> None:
    """The gate. Without it the same run is a green, which is what a live drive reported."""
    result = _drive(tmp_path, _SKIPS, "--require-live")

    assert result.returncode != 0, f"a live drive skipped a test and still reported success:\n{result.stdout}\n{result.stderr}"
    assert "skipped" in (result.stdout + result.stderr).lower(), "the failure does not say what was skipped, so an operator cannot act on it"


def test_a_clean_run_PASSES_under_the_flag(tmp_path: Path) -> None:
    """The other control: a gate that failed every live drive would satisfy the first test and make
    the target unusable."""
    result = _drive(tmp_path, "def test_it_runs() -> None:\n    assert True\n", "--require-live")

    assert result.returncode == 0, f"a fully-passing live drive was refused:\n{result.stdout}\n{result.stderr}"


# --------------------------------------------------------------------------- #
# THE WIRING. The mechanism above is worthless if no target asks for it.
# --------------------------------------------------------------------------- #
