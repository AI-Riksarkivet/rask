"""Environment for the controlplane's tests, set before ANY test module is imported.

`controlplane/__init__.py` builds its app at module level — `app = make_service_app(...)` — and
`make_service_app` reads `settings.api_prefix` once, there. So the prefix is decided by whoever
imports the package FIRST.

`test_controlplane.py` set `RASK_API_PREFIX=/api` inside its `client` fixture and imported the
package on the next line, which is correct only while that fixture is the first thing to touch
`controlplane`. It stopped being true the moment a second test module imported `controlplane.routes`
at collection time: the app was then already built under the code default `/api/v1`, and every route
answered 404 — a failure that points at the routes and is caused by import order.

pytest imports `conftest.py` before the test modules beside it, so setting the environment here fixes
the ordering rather than relying on it. `monkeypatch` cannot do this job: it is function-scoped and
runs long after import.
"""

import os


#: The environment this package's app must be BUILT under, and nothing beyond the build.
#:
#: THE WINDOW IS THE IMPORT, AND ONLY THE IMPORT — the rule `services/compute/tests/conftest.py`
#: already states and follows. Setting these and walking away rewrites the environment for all 21
#: testpaths, not just this one, and the damage lands on whichever service is imported next:
#: `compute`'s own conftest does `setdefault("RASK_API_PREFIX", "/api/v1")`, which is a NO-OP once
#: `/api` is already set, so its app mounts at `/api` while its tests request `/api/v1` and eight of
#: them 404. Measured: `pytest services/controlplane/tests services/compute/tests` fails 8; either
#: directory alone passes. `make test` escapes it only because `testpaths` happens to list compute
#: first — an accident of ordering that `pytest-randomly` is free to undo.
_BUILD_ENV = {
    # The deployed fleet's prefix. The code default is `/api/v1` and nothing deploys it — see
    # `rask-services-fleet` § Gotchas.
    "RASK_API_PREFIX": "/api",
    # The shared `Settings` requires these; controlplane ignores them.
    "RASK_VIEWER_INPUT": "s3://unused",
    "RASK_VIEWER_OUTPUT": "s3://unused",
}
_SAVED = {name: os.environ.get(name) for name in _BUILD_ENV}
for _name, _value in _BUILD_ENV.items():
    os.environ.setdefault(_name, _value)

# IMPORTED HERE ON PURPOSE. `controlplane/__init__.py` builds its app at module level, so the prefix is
# decided by whoever imports the package first; doing it inside this window is what lets the
# environment be handed back immediately rather than held for the rest of the session.
import controlplane as _controlplane  # noqa: E402,F401


for _name, _prev in _SAVED.items():
    if _prev is None:
        os.environ.pop(_name, None)
    else:
        os.environ[_name] = _prev
del _BUILD_ENV, _SAVED, _name, _value, _prev
