"""Guard against frontend↔backend schema drift (#24).

The frontend zones generate their TypeScript catalog/lineage types from the committed
``docs/{catalog,lineage}-openapi.json`` specs (``bun run gen:types:catalog``), which ``make openapi`` dumps
from these FastAPI apps. If a route or response model is added/changed but the committed spec isn't
regenerated, the generated frontend types silently lie. This test fails when a committed spec no longer
covers its live app.

To fix a failure: run ``make openapi`` and commit the updated ``docs/*-openapi.json`` (then re-run the
frontend's ``bun run gen:types:catalog`` if the catalog spec changed).

(Pre-P5 this read ``frontend/apps/web/openapi.json``; that path was retired with apps/web, and the zones now
consume ``docs/*-openapi.json`` directly — so the guard moved here, alongside the ``make openapi-check`` CI
gate that catches the same drift from the other direction.)
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


# Just enough env to CONSTRUCT the catalog Settings at import (mirrors scripts/gen_openapi.py) — the
# placeholders are inert (no lifespan runs for schema generation) and setdefault never overrides a real
# value. Without this the guard only passed when the integration fixtures had already monkeypatched the
# env and left catalog.main cached in sys.modules — `pytest tests/unit` alone was red (import-order
# pollution); the guard must be self-sufficient in any filtered run.
os.environ.setdefault("LANCE_S3_ACCESS_KEY_ID", "spec")
os.environ.setdefault("LANCE_S3_SECRET_ACCESS_KEY", "spec")

_DOCS = Path(__file__).resolve().parents[2] / "docs"
_TS_CLIENT = Path(__file__).resolve().parents[2] / "frontend" / "packages" / "api" / "src" / "generated"


@pytest.mark.parametrize(
    ("module", "spec"),
    [("catalog.main", "catalog-openapi.json"), ("lineage.main", "lineage-openapi.json")],
)
def test_committed_openapi_contract_covers_the_live_app(module: str, spec: str) -> None:
    app = importlib.import_module(module).app
    committed = json.loads((_DOCS / spec).read_text())
    live = app.openapi()

    # The committed snapshot is a superset (dumped with any demo router enabled), so the live app — demo on
    # or off — must be a SUBSET. A live path/schema absent from the committed spec means it went stale.
    missing_paths = set(live["paths"]) - set(committed["paths"])
    assert not missing_paths, f"docs/{spec} is stale — missing paths {sorted(missing_paths)}; run 'make openapi'"

    live_schemas = set(live.get("components", {}).get("schemas", {}))
    missing_schemas = live_schemas - set(committed.get("components", {}).get("schemas", {}))
    assert not missing_schemas, f"docs/{spec} is stale — missing schemas {sorted(missing_schemas)}; run 'make openapi'"


def _string_enums(spec: dict) -> dict[str, list[str]]:
    """Every string enum in a spec's component schemas, keyed by ``Schema.property``.

    Walks properties rather than whole schemas because that is where the drift lands: a new
    ``ControlAction`` member widens ``CatalogControlEvent.action`` and nothing else.
    """
    found: dict[str, list[str]] = {}
    for name, schema in spec.get("components", {}).get("schemas", {}).items():
        for prop, definition in (schema.get("properties") or {}).items():
            members = definition.get("enum")
            if members and all(isinstance(m, str) for m in members):
                found[f"{name}.{prop}"] = members
    return found


# Both specs are swept in ONE test rather than parametrised, because only the catalog carries string
# enums today — a per-spec vacuity guard would fail on lineage for having nothing to check, and
# dropping the guard would let the whole thing pass on an empty sweep.
#
# THE OTHER HOP IS GATED IN THE JS PLANE, not here: spec -> generated TypeScript client is
# `frontend/packages/zone-contract/src/generated-client-freshness.test.ts`, which walks enum members
# for this exact reason. This file owns live app -> committed spec; that one owns committed spec ->
# client. Neither needs to grow the other's half.
_SPECS = [("catalog.main", "catalog-openapi.json", "catalog.ts"), ("lineage.main", "lineage-openapi.json", "lineage.ts")]


def test_committed_openapi_contract_covers_every_live_ENUM_MEMBER() -> None:
    """A schema keeps its name while its enum gains a member, and the name check above cannot see it.

    That is not hypothetical: the five ref-plane ``ControlAction`` members landed on the bus while
    ``docs/catalog-openapi.json`` still stopped at ``table_published``, and the only thing that
    noticed was ``make openapi-check``'s whole-file diff — which a filtered ``pytest tests/unit``
    never runs.
    """
    drifted: dict[str, list[str]] = {}
    swept = 0
    for module, spec, _ in _SPECS:
        live = _string_enums(importlib.import_module(module).app.openapi())
        committed = _string_enums(json.loads((_DOCS / spec).read_text()))
        swept += sum(len(m) for m in live.values())
        for key, members in live.items():
            extra = sorted(set(members) - set(committed.get(key, [])))
            if extra:
                drifted[f"{spec}:{key}"] = extra
    assert swept, "no string enum members were compared — the guard would pass vacuously"
    assert not drifted, f"a committed spec is stale — live-only enum members {drifted}; run 'make openapi'"
