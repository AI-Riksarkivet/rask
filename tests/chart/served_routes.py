"""What a lakehouse service mounts when it boots with the environment the chart renders for its pod.

Each app is built at import from its settings (`catalog.main`, `lineage.main`), so which cron ticks and docs routes it
mounts follows the env its Deployment names. The app is imported in a fresh interpreter under exactly that env: a
worker that already imported the module under another env would answer with that one's routes.

The lifespan does not run, on purpose: every route is registered at import from settings, and the lifespan dials the
store, the bus and the IdP.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


#: Imports the app named by argv[1] and prints every path it mounts.
_PROBE = """
import importlib, json, sys
from fastapi.routing import iter_route_contexts
app = importlib.import_module(sys.argv[1]).app
print(json.dumps(sorted({c.path for c in iter_route_contexts(app.routes) if c.path})))
"""


def served(module: str, env: dict[str, str]) -> frozenset[str]:
    """Boot `module`'s app under `env`, with the interpreter's PATH and HOME where set, and read the paths it mounts."""
    probe = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        env={**{key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}, **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return frozenset(json.loads(probe.stdout.splitlines()[-1]))
