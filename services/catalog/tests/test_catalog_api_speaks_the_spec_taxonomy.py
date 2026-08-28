"""No catalog API module may raise the FLEET's exception taxonomy — the class gate for catalog-api-01.

The defect happened twice before this gate existed: `stores.py` imported `service_kit.exceptions`
in the original audit, and `members.py` repeated it months later (`RV-03`) — each time the module's
errors rendered as 4-key `about:blank#` problem bodies instead of the spec's six-key
`https://lance.org/problems/` envelope, because `register_handlers`'s DomainError handler caught
them before the lance_namespace problem handler could. Two occurrences is a class.

WHY THE SPLIT EXISTS AT ALL, so nobody "fixes" it by merging the taxonomies: the fleet's
`service_kit.exceptions` deliberately emits plain RFC 9457 with no Lance numeric `code` — gateway,
compute and notifications have no Lance contract to cite. The catalog DOES: its clients dispatch on
the spec's 24 codes, so every raise inside `catalog/api/` must come from `lance_namespace`, and the
translation to problem+json belongs to `install_problem_handlers`. The one legitimate import from
the fleet module is `register_handlers` itself (main.py keeps it installed as a net for shared
library code that raises DomainError — e.g. `UserStateConflict` escaping a future call site).

AST, not grep: an alias (`from service_kit import exceptions as exc`) or a multi-name import would
slip a substring check; the parse names the module and the imported names exactly.
"""

from __future__ import annotations

import ast
import pathlib


API_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "api"

#: The only names the catalog's API plane may take from the fleet taxonomy.
_ALLOWED_FROM_FLEET = {"register_handlers"}


def _fleet_taxonomy_imports(path: pathlib.Path) -> list[str]:
    offences: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module == "service_kit.exceptions":
            bad = [alias.name for alias in node.names if alias.name not in _ALLOWED_FROM_FLEET]
            if bad:
                offences.append(f"{path.name}:{node.lineno} imports {bad} from service_kit.exceptions")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("service_kit.exceptions"):
                    offences.append(f"{path.name}:{node.lineno} imports the module wholesale as {alias.asname or alias.name}")
    return offences


def test_the_gate_sees_the_api_plane_at_all() -> None:
    modules = list(API_ROOT.rglob("*.py"))
    assert len(modules) > 20, f"only {len(modules)} modules under catalog/api — the walk is not seeing the plane"


def test_no_api_module_raises_the_fleet_taxonomy() -> None:
    offences = [o for path in sorted(API_ROOT.rglob("*.py")) for o in _fleet_taxonomy_imports(path)]
    assert not offences, (
        "these catalog API modules import the FLEET exception taxonomy — their errors render as "
        "4-key about:blank# bodies with no spec `code`, invisible to every generated Lance client. "
        "Raise `lance_namespace` errors and let install_problem_handlers translate:\n  " + "\n  ".join(offences)
    )
