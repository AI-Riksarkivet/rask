"""What a lakehouse service mounts when it boots with the environment the chart renders for its pod.

Each app is built at import from its settings (`catalog.main`, `lineage.main`), so which cron ticks, Dapr deliveries and
docs routes it mounts follows the env its Deployment names. The app is imported in a fresh interpreter under exactly that
env: a worker that already imported the module under another env would answer with that one's routes.

The lifespan does not run, on purpose: every route and the Dapr subscription are registered at import from settings
(`catalog.main`, `lineage.main`, each `register_*dapr`), and the lifespan dials the store, the bus and the IdP.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from pydantic import BaseModel


#: Imports the app named by argv[1] and prints every mounted path and the routes its Dapr subscription hands the sidecar.
_PROBE = """
import importlib, json, sys
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
app = importlib.import_module(sys.argv[1]).app
paths = sorted({c.path for c in iter_route_contexts(app.routes) if c.path})
deliveries = set()
for s in TestClient(app).get("/dapr/subscribe").json():
    routes = s.get("routes") or {}
    deliveries |= {r for r in (s.get("route"), routes.get("default"), *(rule["path"] for rule in routes.get("rules", []))) if r}
print(json.dumps({"paths": paths, "deliveries": sorted(deliveries)}))
"""


class Served(BaseModel):
    """The paths an app mounts, and the ones Dapr's sidecar delivers its subscriptions to."""

    paths: frozenset[str]
    deliveries: frozenset[str]


def served(module: str, env: dict[str, str]) -> Served:
    """Boot `module`'s app under `env` alone (plus the interpreter's PATH and HOME, where set) and read what it mounts."""
    probe = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        env={**{key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}, **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return Served.model_validate(json.loads(probe.stdout.splitlines()[-1]))
