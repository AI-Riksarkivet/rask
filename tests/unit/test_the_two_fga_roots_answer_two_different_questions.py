"""`fga_root_object` and `fga_default_warehouse_object` are two coordinates, and neither can do the other's job.

[[LH-055]]. They are two settings because they answer two questions, and a single value can satisfy both
only while the estate root happens to be a warehouse:

* **the estate coordinate** — "is this principal a platform-wide admin/observer?" Checked with
  `can_observe_events`, `can_browse_storage`, `can_stage_events`, and bare `reader`/`writer` by compute,
  controlplane, flows, lineage, the viewer and the catalog's store doors.
* **the default warehouse** — "which warehouse does a namespace with no warehouse of its own live in?"
  `fga.parent_object` writes the `parent`/`child` edge to it, and `_create_parent_check` gates top-level
  creation on `can_create_namespace` there whenever `fga_lock_root_create` is set.

THREE THINGS BREAK IF THEY COLLAPSE BACK, all measured rather than argued, and this file gates the two
that a commit can get wrong on its own:

1. `namespace#parent` is typed `[warehouse, namespace]` and every rung a namespace inherits
   `… from parent` must be defined on the parent's type. An `estate:` there is a model error, and —
   the part that does not announce itself — it detaches the 7 top-level namespaces (`bronze`, `silver`,
   `gold`, `bronze-media`, `silver-media`, `models`, `lakehouse`) whose cascade every medallion write
   goes through. Measured on the live store 2026-09-22: `warehouse:lance_catalog` carries those 7
   `child` edges and 10 principal grants.
2. `can_create_namespace` is not an estate relation, and `LANCE_FGA_LOCK_ROOT_CREATE` renders as
   `rask.isRealDeployment` — TRUE in production, FALSE on local k3s (verified on the running catalog:
   `LANCE_FGA_LOCK_ROOT_CREATE=false`). So this one fails in production and passes every local run.

The third — maintenance's reconcile reporting the default warehouse as a permanent ghost once the root
stops being a `warehouse:` — is gated by `test_the_platform_warehouse_is_never_a_ghost.py`, which
drives `build_report` directly.

DERIVED FROM THE MODEL, never a list. The inherited rungs are read out of `model.json`'s
`tupleToUserset` nodes, so a new `… from parent` rung on `namespace` is covered the day it lands.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
_MODEL = REPO / "packages/service-kit/src/service_kit/governed/auth/model.json"
_SETTINGS = REPO / "packages/service-kit/src/service_kit/governed/settings.py"
_FGA_DEPS = REPO / "services/catalog/src/catalog/api/fga_deps.py"

#: The relations `_create_parent_check` names when `fga_lock_root_create` gates a TOP-LEVEL create.
_ROOT_CREATE_RELATIONS = ("can_create_namespace", "can_create_table", "can_create_materialized_view")


def _default(field: str) -> str:
    match = re.search(rf'{field}:\s*str\s*=\s*Field\(default="([^"]+)"', _SETTINGS.read_text(encoding="utf-8"))
    assert match, f"could not read {field}'s default out of service_kit/governed/settings.py"
    return match.group(1)


def _relations(type_name: str) -> dict[str, object]:
    model = json.loads(_MODEL.read_text(encoding="utf-8"))
    for definition in model["type_definitions"]:
        if definition["type"] == type_name:
            return definition.get("relations", {})
    raise AssertionError(f"the model defines no {type_name!r} type — this gate would pass vacuously")


def _inherited_from_parent(type_name: str) -> set[str]:
    """Every relation `type_name` resolves through its `parent` tupleset, at any nesting depth.

    A rung is written as `{"tupleToUserset": {"tupleset": {"relation": "parent"},
    "computedUserset": {"relation": "<rung>"}}}`, and it can sit anywhere inside a `union`/
    `intersection`/`difference` tree — `manage_grants` and `pass_grants` both bury theirs several
    levels down — so the walk is recursive rather than a top-level scan.
    """
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ttu = node.get("tupleToUserset")
            if isinstance(ttu, dict) and ttu.get("tupleset", {}).get("relation") == "parent":
                found.add(ttu["computedUserset"]["relation"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(_relations(type_name))
    return found


def _relations_checked_on_the_root() -> set[str]:
    """Every relation the fleet checks against `settings.fga_root_object`, resolved through constants.

    A door rarely spells the relation at the call: `flows` passes `security.EXECUTE`, the viewer passes
    an imported `BROWSE_STORAGE`, and three services pass a local `READ`. Matching only string literals
    found three of the five and would have let the repoint break `can_browse_storage` and flows' whole
    router silently — so each identifier is resolved to its literal, preferring a definition inside the
    SAME service (two services may legitimately name different rungs `READ`).
    """
    literals: dict[Path, dict[str, str]] = {}
    for path in (REPO / "services").rglob("*.py"):
        if "tests" in path.parts:
            continue
        found = dict(re.findall(r'^([A-Z][A-Z_]*)(?:\s*:\s*Final\[str\])?\s*=\s*"([a-z_]+)"', path.read_text(encoding="utf-8"), re.MULTILINE))
        if found:
            literals[path] = found

    def resolve(name: str, origin: Path) -> set[str]:
        bare = name.rsplit(".", 1)[-1]
        service = origin.parts[origin.parts.index("services") + 1]
        same = {v for p, d in literals.items() if p.parts[p.parts.index("services") + 1] == service for k, v in d.items() if k == bare}
        return same or {v for d in literals.values() for k, v in d.items() if k == bare}

    wanted: set[str] = set()
    for path in (REPO / "services").rglob("*.py"):
        if "tests" in path.parts:
            continue
        body = path.read_text(encoding="utf-8")
        if "fga_root_object" not in body:
            continue
        # WINDOWED to the lines around each mention, never the whole file. A module that gates one door
        # on the root and five on a table mentions both, so a file-wide scan attributes `can_get_metadata`
        # and `can_administer` to the estate root and demands rungs there that nothing checks on it.
        lines = body.splitlines()
        names: set[str] = set()
        for index, line in enumerate(lines):
            if not re.search(r"obj\s*=\s*(?:settings\.)?fga_root_object", line):
                continue
            window = "\n".join(lines[max(0, index - 6) : index + 7])
            names |= set(re.findall(r'relation=([A-Za-z_."]+)', window))
        for name in names:
            if name.startswith('"'):
                wanted.add(name.strip('"'))
            else:
                wanted |= resolve(name, path)
    return wanted


def test_the_default_warehouse_names_a_type_a_namespace_may_hang_off() -> None:
    """Every rung a namespace inherits `from parent` must exist on the default warehouse's type."""
    warehouse_type = _default("fga_default_warehouse_object").split(":", 1)[0]
    inherited = _inherited_from_parent("namespace")
    assert inherited, "no `… from parent` rungs parsed off `namespace` — this gate would pass vacuously"

    missing = sorted(inherited - set(_relations(warehouse_type)))
    assert not missing, (
        f"a top-level namespace parents onto {_default('fga_default_warehouse_object')!r}, whose type "
        f"{warehouse_type!r} defines none of {missing} — the model refuses the tuple, and the cascade "
        f"that reaches bronze/silver/gold goes with it."
    )


def test_the_default_warehouse_can_answer_the_root_create_lock() -> None:
    """`fga_lock_root_create` is ON in production and OFF locally, so this has no local reproduction."""
    assert "settings.fga_default_warehouse_object, relation" in _FGA_DEPS.read_text(encoding="utf-8"), (
        "the root-create lock no longer names `fga_default_warehouse_object` — if it names the estate "
        "root instead, top-level creation fails in every real deployment and passes every local run."
    )
    warehouse_type = _default("fga_default_warehouse_object").split(":", 1)[0]
    defined = set(_relations(warehouse_type))
    missing = sorted(set(_ROOT_CREATE_RELATIONS) - defined)
    assert not missing, f"{warehouse_type!r} defines none of {missing}, which the root-create lock checks on it"


def test_the_estate_coordinate_defines_every_rung_the_fleet_checks_on_it() -> None:
    """The other direction: whatever `fga_root_object` names must answer the checks aimed at it.

    Read off the services rather than listed here, so a new estate-gated door is covered by landing
    rather than by remembering. Only the six modules that check against `fga_root_object` count — a
    relation checked on a table is not an estate rung.
    """
    root_type = _default("fga_root_object").split(":", 1)[0]
    wanted = _relations_checked_on_the_root()
    assert len(wanted) >= 5, (
        f"only {sorted(wanted)} parsed off the estate-root call sites. Six modules check against it "
        f"(catalog events/stores/me/projects/access_admin, lineage, compute, controlplane, flows, the "
        f"viewer) — an under-derivation makes this gate pass while a door it missed is broken."
    )
    missing = sorted(wanted - set(_relations(root_type)))
    assert not missing, (
        f"the fleet checks {missing} on {_default('fga_root_object')!r}, whose type {root_type!r} does "
        f"not define them — OpenFGA answers `relation not found` and the door fails closed for everyone."
    )
