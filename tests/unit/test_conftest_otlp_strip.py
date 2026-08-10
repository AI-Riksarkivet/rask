"""The root conftest must strip the harness's OTLP variables BEFORE collection imports anything.

THE BUG THIS PINS, and it is an ordering bug rather than a logic one. `dagger call test` hung twice
(abandoned at 22 and 56 minutes) with the worker asleep in `hrtimer_nanosleep`: Dagger injects
`OTEL_EXPORTER_OTLP_ENDPOINT` for its own telemetry, `services/gateway` calls `setup_otel(...)` at
MODULE scope and opts in through that endpoint alone, and the resulting exporter retried against a
collector that rejects application metrics, with exponential backoff, for every app any test built.

The root conftest was written to close it and did not, because of the order pytest actually runs in:

    load rootdir conftest  →  COLLECT (imports every test module)  →  run first test

A session-scoped autouse fixture executes at the THIRD step. The exporter is built at the second. So
the original fixture removed the variable from an environment that had already been read, and the hang
survived the fix that claimed it.

WHY A SUBPROCESS. The property is about what the environment looks like DURING IMPORT of a module
collected by pytest, and that moment is already past by the time any test body runs — an in-process
assertion cannot observe it. So a real pytest is driven in a child process with the variable injected,
running a generated module that records the environment at its own import time. Reproduction rather
than argument: this is the same reasoning `tests/unit/test_e2e_collection_gate.py` uses when it shells
out to `pytest --collect-only`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every variable the root conftest claims to strip, driven one per case so a partial strip names the
#: exact variable that survived instead of failing as an opaque set difference. Kept in step with
#: `_HARNESS_OTLP_VARS` by :func:`test_this_suite_covers_every_variable_the_conftest_strips`.
HARNESS_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
)

#: A module that records the variable AT IMPORT — the moment a module-scope `setup_otel` would read
#: it — and asserts on that recording from the test body. Reading it in the body instead would pass
#: even with the strip back in the too-late fixture, which is precisely the bug.
_PROBE = """
import os
SEEN_AT_IMPORT = os.environ.get({var!r})


def test_the_variable_was_already_gone_when_this_module_was_imported():
    assert SEEN_AT_IMPORT is None, (
        "{var} was still set at COLLECTION time: the root conftest strips it too late, so a "
        "module-scope setup_otel has already wired a live exporter. See conftest.py."
    )
"""


@pytest.fixture
def probe_dir() -> Iterator[Path]:
    """A scratch directory INSIDE the repo, because that is what makes the root conftest apply.

    pytest collects conftests by walking UP from each argument's directory, so a probe in the usual
    `tmp_path` (outside the repo) is run with no root conftest at all — the child would pass or fail
    for reasons having nothing to do with the file under test. The leading dot keeps the directory out
    of any normal collection (`norecursedirs`), and `testpaths` is an explicit list that never names it.
    """
    path = REPO_ROOT / f".otlp-probe-{os.getpid()}"
    path.mkdir(exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.mark.parametrize("var", HARNESS_VARS)
def test_the_harness_variable_is_gone_before_a_collected_module_is_imported(var: str, probe_dir: Path) -> None:
    probe = probe_dir / "test_otlp_probe.py"
    probe.write_text(_PROBE.format(var=var), encoding="utf-8")

    result = subprocess.run(
        # `-c` pins the ROOT config so the root conftest is the one under test.
        [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider", "-c", str(REPO_ROOT / "pyproject.toml"), str(probe)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, var: "http://a-collector-that-must-never-be-dialled:4318"},
    )

    assert result.returncode == 0, f"{var} survived into collection:\n{result.stdout}\n{result.stderr}"


def test_the_probe_itself_can_fail(probe_dir: Path) -> None:
    """The harness's own non-vacuity check. If the child run were mis-wired — wrong config, conftest
    never loaded, probe never collected — every case above would go green while proving nothing. Here
    the child is given a variable the conftest does NOT strip, so the probe MUST fail; if it passes,
    the other seven results are worthless."""
    probe = probe_dir / "test_otlp_probe.py"
    probe.write_text(_PROBE.format(var="OTEL_A_VARIABLE_THE_CONFTEST_DOES_NOT_STRIP"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider", "-c", str(REPO_ROOT / "pyproject.toml"), str(probe)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "OTEL_A_VARIABLE_THE_CONFTEST_DOES_NOT_STRIP": "set"},
    )

    assert result.returncode != 0, f"the probe cannot fail, so it proves nothing:\n{result.stdout}\n{result.stderr}"


def test_this_suite_covers_every_variable_the_conftest_strips() -> None:
    """A variable added to the conftest's tuple and not to this one would be stripped by code no test
    exercises — the class of gap that let the original ordering bug ship green.

    Loaded by PATH: `--import-mode=importlib` means the root conftest is not importable as a top-level
    module named `conftest`, and pytest's own copy of it is not in `sys.modules` under that name."""
    spec = importlib.util.spec_from_file_location("_root_conftest_under_test", REPO_ROOT / "conftest.py")
    assert spec and spec.loader
    root_conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(root_conftest)

    assert set(root_conftest._HARNESS_OTLP_VARS) == set(HARNESS_VARS)
