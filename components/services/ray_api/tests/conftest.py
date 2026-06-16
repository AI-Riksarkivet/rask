"""Test isolation for ray-api. The app singleton bakes Settings at import via
make_service_app → build_settings → load_dotenv (which may read the dev .env).

RAY_DASHBOARD_URL is forced to an unreachable address so build_client returns
None and the dashboard HTTP calls fail fast — the endpoints then exercise their
offline (ok=False) paths deterministically. RASK_API_PREFIX/VIEWER_INPUT/OUTPUT
are defaulted so Settings validates at import; eager import bakes them in."""

import os


os.environ["RAY_DASHBOARD_URL"] = "http://127.0.0.1:9"  # discard/closed port → refused fast
os.environ.setdefault("RASK_API_PREFIX", "/api/v1")
os.environ.setdefault("RASK_VIEWER_INPUT", "/dev/null")
os.environ.setdefault("RASK_VIEWER_OUTPUT", "/dev/null")

import ray_api as _ra  # noqa: F401
