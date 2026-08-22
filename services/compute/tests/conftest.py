"""Test isolation for the compute service. The app singleton bakes Settings at import
via make_service_app → build_settings → load_dotenv (which may read the dev .env).

RAY_DASHBOARD_URL is forced to an unreachable address so build_client returns
None and the dashboard HTTP calls fail fast — the endpoints then exercise their
offline (ok=False) paths deterministically. RASK_API_PREFIX is defaulted so the
mount prefix is stable; eager import bakes it in."""

import os


# THE WINDOW IS THE IMPORT, AND ONLY THE IMPORT. `make_service_app` bakes `build_settings()` into the
# module singleton and the lifespan closes over it, so once `compute` is imported nothing reads these
# variables again — while pytest COLLECTS every module before running any test, so leaving the
# mutation in place rewrote the environment for all 21 testpaths, including the ones that run before
# this directory. Restoring immediately after the import keeps the determinism this file exists for
# and gives the rest of the session its environment back.
_SAVED = {name: os.environ.get(name) for name in ("RAY_DASHBOARD_URL", "RASK_API_PREFIX")}
os.environ["RAY_DASHBOARD_URL"] = "http://127.0.0.1:9"  # discard/closed port → refused fast
os.environ.setdefault("RASK_API_PREFIX", "/api/v1")

import compute as _compute  # noqa: E402,F401


for _name, _prev in _SAVED.items():
    if _prev is None:
        os.environ.pop(_name, None)
    else:
        os.environ[_name] = _prev
del _SAVED, _name, _prev
