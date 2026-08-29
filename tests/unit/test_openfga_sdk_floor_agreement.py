"""Every member that speaks to OpenFGA declares the SAME openfga-sdk floor as service-kit.

Commit bbecc94f made the FGA write path depend on ``on_duplicate_writes=IGNORE`` (SDK >= 0.10.4)
and bumped service-kit's floor — but catalog and medallion kept ``>=0.9``. The root lock resolves
one version for the workspace, so the drift is invisible locally; it bites the moment a member is
resolved on its own floor (a solo ``uv sync --package``, a downstream consumer), where an 0.9 SDK
satisfies the manifest and the duplicate-tolerant write silently loses its server-side guarantee.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]


def _openfga_floor(pyproject: Path) -> str | None:
    project = tomllib.loads(pyproject.read_text())["project"]
    deps = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():  # service-kit carries it in the `governed` extra
        deps.extend(group)
    for dep in deps:
        m = re.fullmatch(r"openfga-sdk\s*>=\s*([\w.]+)", dep)
        if m:
            return m.group(1)
    return None


def test_every_openfga_consumer_declares_service_kits_floor() -> None:
    anchor = _openfga_floor(_ROOT / "packages/service-kit/pyproject.toml")
    assert anchor is not None, "service-kit no longer declares an openfga-sdk floor — this gate's anchor moved"

    floors = {member: _openfga_floor(_ROOT / member / "pyproject.toml") for member in ("services/catalog", "services/medallion")}
    drifted = {member: floor for member, floor in floors.items() if floor != anchor}
    assert not drifted, f"openfga-sdk floors drifted from service-kit's >={anchor}: {drifted}"
