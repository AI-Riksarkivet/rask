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


# The deployed fleet's prefix. The code default is `/api/v1` and nothing deploys it — see
# `rask-services-fleet` § Gotchas.
os.environ.setdefault("RASK_API_PREFIX", "/api")
# The shared `Settings` requires these; controlplane ignores them.
os.environ.setdefault("RASK_VIEWER_INPUT", "s3://unused")
os.environ.setdefault("RASK_VIEWER_OUTPUT", "s3://unused")
