#!/usr/bin/env python
"""Generate the committed OpenAPI specs for the REST services from their live FastAPI apps.

The catalog + lineage REST surfaces — including every net-new endpoint (warehouse admin,
client-direct commit, materialized views, …) — are defined by the code, not a hand-written spec. This dumps
FastAPI's generated OpenAPI to ``docs/<service>-openapi.json`` so there is one canonical, reviewable spec of
what we actually serve, and CI fails if it drifts from the routes (``make openapi`` regenerates; the CI step
runs this then ``git diff --exit-code``). Run: ``uv run scripts/gen_openapi.py``.

Only enough env to CONSTRUCT ``Settings`` at import is set — the app lifespans (real S3/OIDC/FGA) never run
for schema generation, so the placeholders below are inert. The frontend types are generated from the committed spec
(``bun run gen:types``).
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("LANCE_S3_ACCESS_KEY_ID", "spec")
os.environ.setdefault("LANCE_S3_SECRET_ACCESS_KEY", "spec")
os.environ.setdefault("RASK_DATABASE_HOST", "spec")
os.environ.setdefault("RASK_DATABASE_NAME", "spec")
os.environ.setdefault("RASK_DATABASE_CREDENTIALS_DIR", "spec")

_DOCS = _ROOT / "docs"

#: (import path of the FastAPI ``app``, output filename) for each REST service we publish a spec for.
_SERVICES = [
    ("catalog.main", "catalog-openapi.json"),
    ("lineage.main", "lineage-openapi.json"),
]


def _dump(app: Any, path: Path) -> int:
    """Write ``app.openapi()`` deterministically (sorted keys), whole or not at all; return the path count for a summary line.

    Written to a file beside ``path`` and renamed over it, so an interrupted run leaves the committed spec as it was.
    """
    spec = app.openapi()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as staged:
        staged.write(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    staged_path = Path(staged.name)
    staged_path.chmod(0o644)  # a temporary file is created 0600; the spec is read by everyone, as a checkout writes it
    staged_path.replace(path)
    return len(spec.get("paths", {}))


def main() -> None:
    import importlib

    for module_path, filename in _SERVICES:
        app = importlib.import_module(module_path).app
        count = _dump(app, _DOCS / filename)
        print(f"wrote docs/{filename} ({count} paths)")


if __name__ == "__main__":
    main()
