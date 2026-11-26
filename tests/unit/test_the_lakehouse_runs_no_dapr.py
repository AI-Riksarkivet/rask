"""The lakehouse runs no Dapr: the lint refuses the name in every code tree but its guards, and nowhere else ([[XC-199]]).

Driven on a tree of the test's own with no `.git`, as `dagger call lint` sees the source: the name planted in a fixture
chart's template, spelled in capitals in a Dagger lane, and in a file holding a byte that is not UTF-8 is refused; the same name in the ruff ban that keeps it out,
in `docs/`, where decisions are recorded, and in a vendored upstream file is not. This file names it to plant it, so the
lint leaves it out.
"""

from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from tests.script_module import script


_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

_PLANTED: Final = frozendict(
    {
        "deploy/fixtures/nats/chart/templates/bus.yaml": '    dapr.io/enabled: "true"\n',
        ".dagger/engine.go": "// DAPR control plane for the lane\n",
        # A lone surrogate is written as the one raw byte it escapes, so the file is not UTF-8.
        "scripts/latin1.sh": "# caf\udce9 dapr sidecar\n",
        "pyproject.toml": '"dapr".msg = "banned"\n',
        "docs/adr/0207-the-lakehouse-runs-no-dapr.md": "The lakehouse runs no Dapr.\n",
        "deploy/fixtures/nats/chart/charts/nats/values.yaml": "# dapr\n",
        "Makefile": "\tuv run python scripts/the_lakehouse_runs_no_dapr.py\n",
    }
)


@pytest.fixture
def lint(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """`scripts/the_lakehouse_runs_no_dapr.py`."""
    return script("the_lakehouse_runs_no_dapr", monkeypatch)


def test_the_lint_reports_each_place_code_outside_its_guards_names_dapr(lint: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name, text in _PLANTED.items():
        planted = tmp_path / name
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_bytes(text.encode("utf-8", errors="surrogateescape"))

    assert lint.main(["--root", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        ".dagger/engine.go:1: 'DAPR'",
        "deploy/fixtures/nats/chart/templates/bus.yaml:1: 'dapr'",
        "scripts/latin1.sh:1: 'dapr'",
    ]
