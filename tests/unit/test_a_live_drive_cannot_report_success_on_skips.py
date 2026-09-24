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

import re
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _ROOT / "Makefile"
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


def test_the_SAME_run_is_green_without_the_flag(tmp_path: Path) -> None:
    """The control, and it is what keeps the flag opt-in: an ordinary offline collection of these
    suites must behave exactly as before, because skipping is how they stay runnable at all."""
    result = _drive(tmp_path, _SKIPS)

    assert result.returncode == 0, f"a plain run was broken by the option's presence:\n{result.stdout}\n{result.stderr}"


def test_a_clean_run_PASSES_under_the_flag(tmp_path: Path) -> None:
    """The other control: a gate that failed every live drive would satisfy the first test and make
    the target unusable."""
    result = _drive(tmp_path, "def test_it_runs() -> None:\n    assert True\n", "--require-live")

    assert result.returncode == 0, f"a fully-passing live drive was refused:\n{result.stdout}\n{result.stderr}"


# --------------------------------------------------------------------------- #
# THE WIRING. The mechanism above is worthless if no target asks for it.
# --------------------------------------------------------------------------- #


def _recipe(target: str) -> str:
    """The recipe lines of one Makefile target."""
    text = _MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:.*?\n((?:\t.*\n|#.*\n|\n)*)", text, re.MULTILINE)
    assert match, f"{target} is not a Makefile target any more — this gate is measuring something that moved"
    return match.group(1)


def test_the_DUMMY_LANE_target_requires_every_variable_its_suite_needs() -> None:
    """The measured case. Its siblings guard every variable they forward; this one guarded one of three,
    so an operator with a single port-forward drove the cascade prover and got a green out of it."""
    recipe = _recipe("e2e-dummy-lane")
    missing = [var for var in ("LANCE_E2E_CATALOG_URL", "LANCE_E2E_ADMIN_TOKEN", "LANCE_E2E_LINEAGE_URL") if f'test -n "$({var})"' not in recipe]

    assert not missing, f"e2e-dummy-lane runs without {missing}, so the suite skips its way to a zero exit"
    unforwarded = [var for var in ("LANCE_E2E_ADMIN_TOKEN", "LANCE_E2E_LINEAGE_URL") if f"{var}=$({var})" not in recipe]
    assert not unforwarded, f"e2e-dummy-lane guards {unforwarded} and then does not pass them to pytest, so the suite skips anyway"


def test_the_DUMMY_LANE_target_asks_for_a_full_run() -> None:
    """A guarded variable removes the skips it causes and no others — a token that is not a project
    admin skips with every variable set."""
    assert "--require-live" in _recipe("e2e-dummy-lane"), "e2e-dummy-lane can still report success on a run where nothing ran"


@pytest.mark.parametrize("target", ["e2e-dummy-lane"])
def test_the_target_still_names_its_suite(target: str) -> None:
    """Anti-vacuity: the assertions above all read one recipe, and a recipe that stopped invoking
    pytest would satisfy none of them for the wrong reason."""
    assert "pytest tests/e2e-py" in _recipe(target), f"{target} no longer drives the e2e suite"
