"""The no-client lint refuses a departed client's name, though no tree in the repository holds that client ([[XC-193]]).

The lint learns a hosted client's name from that client's tree, and a client that has left the repository has none, so
the lint carries the departed names as a list of its own. Measured 2026-11-06 at `0faa294c3`: with only the names read
off the tree, `make lint` and CI's lint leg passed while lakehouse files named departed clients' accounts and images.

Driven on a tree of the test's own, which holds no client: a lakehouse test names departed clients in each position a
name takes (an account, an image, an object, a module, a path inside a client's tree, a departed tree's path, a former
zone of the console, a plain name), and uses the same words in ordinary English, which the lint must let through.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("lakehouse_names_no_client", REPO_ROOT / "scripts" / "lakehouse_names_no_client.py")
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["lakehouse_names_no_client"] = gate
_SPEC.loader.exec_module(gate)

_SUITE = """\
ACCOUNT = "rask-sa-flows"
IMAGE = "flows:dev"
OBJECT = "rask-ray-compute"
# The job door lives in compute.py, and viewer/api/security.py publishes its deps.
# runners/dummy and frontend/microfrontends/home/src/lib/x.ts are gone, and so is the annotator.
# Data flows through the catalog, and a job's own compute writes it.
"""


def test_the_lint_reports_each_place_a_lakehouse_file_names_a_departed_client(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for tree in ("services", "packages", "frontend", "deploy/fixtures"):
        (tmp_path / tree).mkdir(parents=True)
    suite = tmp_path / "tests/integration/test_names.py"
    suite.parent.mkdir(parents=True)
    suite.write_text(_SUITE, encoding="utf-8")

    assert gate.main(["--root", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "tests/integration/test_names.py:1: 'rask-sa-flows'",
        "tests/integration/test_names.py:2: 'flows:dev'",
        "tests/integration/test_names.py:3: 'rask-ray-compute'",
        "tests/integration/test_names.py:4: 'compute.py'",
        "tests/integration/test_names.py:4: 'viewer/api/'",
        "tests/integration/test_names.py:5: 'runners/dummy'",
        "tests/integration/test_names.py:5: 'microfrontends/home'",
        "tests/integration/test_names.py:5: 'annotator'",
    ]
