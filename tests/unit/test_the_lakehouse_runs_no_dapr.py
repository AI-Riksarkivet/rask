"""The lakehouse runs no Dapr: the lint refuses the name in every code tree but its guards, and nowhere else ([[XC-199]]).

Driven on a tree of the test's own with no `.git`, as `dagger call lint` sees the source: the name planted in a fixture
chart's template and, spelled in capitals, in a Dagger lane is refused; the same name in the ruff ban that keeps it out,
in `docs/`, where decisions are recorded, and in a vendored upstream file is not. This file names it to plant it, so the
lint leaves it out.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

_PLANTED = {
    "deploy/fixtures/nats/chart/templates/bus.yaml": '    dapr.io/enabled: "true"\n',
    ".dagger/engine.go": "// DAPR control plane for the lane\n",
    "pyproject.toml": '"dapr".msg = "banned"\n',
    "docs/adr/0207-the-lakehouse-runs-no-dapr.md": "The lakehouse runs no Dapr.\n",
    "deploy/fixtures/nats/chart/charts/nats/values.yaml": "# dapr\n",
    "Makefile": "\tuv run python scripts/the_lakehouse_runs_no_dapr.py\n",
}


@pytest.fixture
def lint(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """`scripts/the_lakehouse_runs_no_dapr.py`, imported the way `python scripts/...` resolves its sibling `_files_under`."""
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("the_lakehouse_runs_no_dapr", _SCRIPTS / "the_lakehouse_runs_no_dapr.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_the_lint_reports_each_place_code_outside_its_guards_names_dapr(lint: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name, text in _PLANTED.items():
        planted = tmp_path / name
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(text, encoding="utf-8")

    assert lint.main(["--root", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        ".dagger/engine.go:1: 'DAPR'",
        "deploy/fixtures/nats/chart/templates/bus.yaml:1: 'dapr'",
    ]
