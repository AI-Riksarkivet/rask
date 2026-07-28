"""Test isolation for the ray service. The app singleton bakes Settings at import
via make_service_app → build_settings → load_dotenv (which may read the dev .env).

RAY_DASHBOARD_URL is forced to an unreachable address so build_client returns
None and the dashboard HTTP calls fail fast — the endpoints then exercise their
offline (ok=False) paths deterministically. RASK_API_PREFIX is defaulted so the
mount prefix is stable; eager import bakes it in."""

import os


os.environ["RAY_DASHBOARD_URL"] = "http://127.0.0.1:9"  # discard/closed port → refused fast
os.environ.setdefault("RASK_API_PREFIX", "/api/v1")

import ray_api as _ra  # noqa: F401
