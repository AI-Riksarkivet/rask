"""The code names no reference product: the lint refuses one in every code tree and nowhere else (owner, 2026-11-07).

Driven on a tree of the test's own with no `.git`, as `dagger call lint` sees the source: the name planted in a service
module and, spelled in capitals, in a test is refused; the same name in `docs/`, where research is recorded, in a
vendored upstream file and in a dependency's installed copy is not judged. This file names the product to plant it, so
the lint leaves it out, as the no-client lint leaves out its own gate tests.
"""

from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from tests.script_module import script


_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

_PLANTED: Final = frozendict(
    {
        "services/catalog/src/catalog/doors.py": "LIMIT = 1000  # the ceiling Lakekeeper's batch check uses\n",
        "tests/unit/test_doors.py": "# LAKEKEEPER parity\n",
        "docs/research.md": "Lakekeeper's console keeps its notifications in the browser.\n",
        "deploy/fixtures/nats/chart/charts/nats/values.yaml": "# vendored upstream: lakekeeper\n",
        "frontend/console/node_modules/pkg/index.js": "// lakekeeper\n",
    }
)


@pytest.fixture
def lint(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """`scripts/code_names_no_reference_product.py`."""
    return script("code_names_no_reference_product", monkeypatch)


def test_the_lint_reports_each_place_code_names_a_reference_product(lint: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name, text in _PLANTED.items():
        planted = tmp_path / name
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(text, encoding="utf-8")

    assert lint.main(["--root", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "services/catalog/src/catalog/doors.py:1: 'Lakekeeper'",
        "tests/unit/test_doors.py:1: 'LAKEKEEPER'",
    ]
