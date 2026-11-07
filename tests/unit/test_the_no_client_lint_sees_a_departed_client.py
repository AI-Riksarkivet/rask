"""The no-client lint refuses a departed client's name, though no tree in the repository holds that client ([[XC-193]]).

The lint learns a hosted client's name from that client's tree, and a client that has left the repository has none, so
the lint carries the departed names as a list of its own. Measured 2026-11-06 at `0faa294c3`: with only the names read
off the tree, `make lint` and CI's lint leg passed while lakehouse files named departed clients' accounts and images.

Driven on a tree of the test's own, which holds the console and no client: a lakehouse test names departed clients in
each position a name takes (an account, an image, an object, a module, a path inside a client's tree, a departed tree's
path, a former zone of the console, a plain name), the retired notifications inbox and its SMTP relay ([[XC-195]]), and
the console, which is the lakehouse's own UI and no client, and uses the same words in ordinary English, which the lint
must let through. The console's tree is the lakehouse's, so a departed client named there is refused too.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

_SUITE = """\
ACCOUNT = "rask-sa-flows"
IMAGE = "flows:dev"
OBJECT = "rask-ray-compute"
# The job door lives in compute.py, and viewer/api/security.py publishes its deps.
# runners/dummy and frontend/microfrontends/home/src/lib/x.ts are gone, and so is the annotator.
# Data flows through the catalog, and a job's own compute writes it.
INBOX = "rask-sa-notifications"
RELAY = "rask-mailpit"
# The console signs in as `console`'s client rask-console, and a notification rule names no client.
"""


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """`scripts/lakehouse_names_no_client.py`, imported the way `python scripts/...` resolves its sibling `_files_under`."""
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("lakehouse_names_no_client", _SCRIPTS / "lakehouse_names_no_client.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_the_lint_reports_each_place_a_lakehouse_file_names_a_departed_client(gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for tree in ("services", "packages", "frontend/console", "deploy/fixtures"):
        (tmp_path / tree).mkdir(parents=True)
    (tmp_path / "frontend/console/package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "frontend/console/inbox.ts").write_text('const BASE = "services/notifications/api";\n', encoding="utf-8")
    suite = tmp_path / "tests/integration/test_names.py"
    suite.parent.mkdir(parents=True)
    suite.write_text(_SUITE, encoding="utf-8")

    assert gate.main(["--root", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "frontend/console/inbox.ts:1: 'services/notifications'",
        "tests/integration/test_names.py:1: 'rask-sa-flows'",
        "tests/integration/test_names.py:2: 'flows:dev'",
        "tests/integration/test_names.py:3: 'rask-ray-compute'",
        "tests/integration/test_names.py:4: 'compute.py'",
        "tests/integration/test_names.py:4: 'viewer/api/'",
        "tests/integration/test_names.py:5: 'runners/dummy'",
        "tests/integration/test_names.py:5: 'microfrontends/home'",
        "tests/integration/test_names.py:5: 'annotator'",
        "tests/integration/test_names.py:7: 'rask-sa-notifications'",
        "tests/integration/test_names.py:8: 'mailpit'",
    ]
