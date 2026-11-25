"""`app.dependency_overrides` must reach the settings a dependency actually uses.

open_fastapi-audit — "`get_settings()` is called directly inside a dependency and across service code,
so `app.dependency_overrides` silently is not the seam".

`get_namespace` took only `request` and then called `get_settings()` in its body. FastAPI's override
map is keyed on the DEPENDENCY CALLABLE, so overriding `request_settings` reached every route that
declared `SettingsDep` — and not this one, which read the real env-derived settings regardless. A test
that overrode settings and exercised a warehouse-routed path would have been silently green against
the wrong configuration.

`dependencies.md`: "`app.dependency_overrides` swaps a dep at the registration key — no monkey-patching
of internals, no global state". A dependency that calls the provider directly opts out of that
guarantee without saying so.

WHAT THIS IS AND IS NOT, because the audit is careful and the gate should be too: there is no current
victim. No catalog test exercises `get_namespace` under an override today, so nothing is green on an
unexecuted branch right now. This is a latent DI-seam defect — the kind that is cheap now and
expensive the first time someone trusts the override.

FastAPI caches `request_settings` per request, so a route already declaring `SettingsDep` pays nothing for
the injection.
"""

from fastapi import FastAPI, Request

from catalog.api import dependencies as deps
from tests.catalog_settings import catalog_settings


def test_an_override_actually_changes_what_the_dependency_sees() -> None:
    """End to end through FastAPI's own resolution, which is the only proof that matters."""
    from fastapi.testclient import TestClient

    from catalog.core.config import Settings

    app = FastAPI()

    @app.get("/probe/{id}")
    async def probe(request: Request, ns: deps.NamespaceDep) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    seen: dict[str, object] = {}

    def _settings() -> Settings:
        s = catalog_settings(LANCE_REST_IMPL="dir")
        seen["settings"] = s
        return s

    class _Connections:
        def connection(self, _settings: Settings, **_: object) -> object:
            return object()

    app.dependency_overrides[deps.request_settings] = _settings
    app.state.connections = _Connections()

    response = TestClient(app).get("/probe/lance_catalog$thing")
    assert response.status_code == 200, response.text
    assert seen, "the override was never consulted — `get_namespace` read the process settings instead, which is exactly the seam this finding is about"
