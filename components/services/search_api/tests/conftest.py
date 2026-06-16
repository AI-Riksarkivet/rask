"""Test isolation for the search-api test package.

`make_service_app` is called at `search_api` module import time and bakes a
`Settings` instance into the app.  `build_settings()` calls `load_dotenv()`
which re-reads the developer's local `.env` and may set `HCP_ENDPOINT` there.

`HCP_ENDPOINT` is forced unconditionally to `""` so `_build_s3` returns `None`
and `test_thumb_without_s3_returns_503` gets the expected 503 — even in CI
where a real `HCP_ENDPOINT` may be exported in the environment.

`RASK_API_PREFIX`, `RASK_VIEWER_INPUT`, and `RASK_VIEWER_OUTPUT` are defaulted
(not forced) so CI may legitimately override them, but collection does not fail
when they are absent.

Same intent as `components/services/viewer/tests/conftest.py` (which uses an
autouse monkeypatch fixture; this one mutates os.environ at module level because
search_api builds its app singleton at import time).
"""

import os


os.environ["HCP_ENDPOINT"] = ""          # force: the module-level singleton must bake hcp_endpoint=""
os.environ.setdefault("RASK_API_PREFIX", "/api/v1")
os.environ.setdefault("RASK_VIEWER_INPUT", "/dev/null")
os.environ.setdefault("RASK_VIEWER_OUTPUT", "/dev/null")

# Force-import the module NOW, while HCP_ENDPOINT="" is in os.environ, so the
# module-level singleton (`app = make_service_app(…)`) bakes in hcp_endpoint="".
# All subsequent `from search_api import app` calls hit Python's module cache
# and receive the same app object — regardless of what monkeypatch does later.
import search_api as _sa  # noqa: F401
