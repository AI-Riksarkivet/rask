#!/usr/bin/env python
"""Generate the committed OpenAPI specs for the REST services from their live FastAPI apps.

The catalog + lineage REST surfaces — including every net-new endpoint (model promotion, warehouse admin,
client-direct commit, materialized views, …) — are defined by the code, not a hand-written spec. This dumps
FastAPI's generated OpenAPI to ``docs/<service>-openapi.json`` so there is one canonical, reviewable spec of
what we actually serve, and CI fails if it drifts from the routes (``make openapi`` regenerates; the CI step
runs this then ``git diff --exit-code``). Run: ``uv run scripts/gen_openapi.py``.

Only enough env to CONSTRUCT ``Settings`` at import is set — the app lifespans (real S3/OIDC/FGA) never run
for schema generation, so the placeholders below are inert. The demo router is force-enabled at dump time so
the committed spec is a SUPERSET of any live config (the premise ``tests/unit/test_openapi_contract.py``
asserts against), and the frontend types generated from it (``bun run gen:types``) keep the demo shapes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "services"))  # `uv run` doesn't apply the pytest pythonpath
os.environ.setdefault("LANCE_S3_ACCESS_KEY_ID", "spec")
os.environ.setdefault("LANCE_S3_SECRET_ACCESS_KEY", "spec")
os.environ.setdefault("LINEAGE_DEMO_DATA_ENABLED", "true")  # spec = superset: demo routes always dumped

_DOCS = _ROOT / "docs"

#: (import path of the FastAPI ``app``, output filename) for each REST service we publish a spec for.
_SERVICES = [
    ("catalog.main", "catalog-openapi.json"),
    ("lineage.main", "lineage-openapi.json"),
]


def _dump(app: Any, path: Path) -> int:
    """Write ``app.openapi()`` deterministically (sorted keys); return the path count for a summary line."""
    spec = app.openapi()
    path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    return len(spec.get("paths", {}))


def main() -> None:
    import importlib

    for module_path, filename in _SERVICES:
        app = importlib.import_module(module_path).app
        count = _dump(app, _DOCS / filename)
        print(f"wrote docs/{filename} ({count} paths)")


if __name__ == "__main__":
    main()
