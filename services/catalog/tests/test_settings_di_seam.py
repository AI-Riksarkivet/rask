"""`app.dependency_overrides` must reach the settings a dependency actually uses.

open_fastapi-audit — "`get_settings()` is called directly inside a dependency and across service code,
so `app.dependency_overrides` silently is not the seam".

`get_namespace` took only `request` and then called `get_settings()` in its body. FastAPI's override
map is keyed on the DEPENDENCY CALLABLE, so overriding `get_settings` reached every route that
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

FastAPI caches `get_settings` per request, so a route already declaring `SettingsDep` pays nothing for
the injection.
"""

from __future__ import annotations

import inspect

# Module level, NOT inside the test: `from __future__ import annotations` makes every annotation a
# string, and FastAPI resolves them against the DEFINING module's namespace. A function-local
# import leaves `Request` unresolvable, and FastAPI then treats it as a query parameter — which
# presents as a 422 about a missing `request` field rather than as an import problem.
from fastapi import FastAPI, Request

from catalog.api import dependencies as deps


def test_get_namespace_takes_its_settings_by_injection() -> None:
    sig = inspect.signature(deps.get_namespace)
    assert "settings" in sig.parameters, (
        "get_namespace calls get_settings() in its body, so app.dependency_overrides cannot reach it — "
        "an override that works on every SettingsDep route silently misses this one"
    )


def test_the_dependency_no_longer_calls_the_provider_directly() -> None:
    """The signature alone is not the property: a parameter plus a body call would still read the real
    settings for anything the parameter did not cover."""
    # Comments stripped: the fix's own note EXPLAINS the call it removed, so a raw substring match
    # fails against the corrected code. Same false-positive class as grepping for a docstring claim
    # that a correction quotes in order to correct it.
    source = inspect.getsource(deps.get_namespace)
    code = "\n".join(line for line in source.split("\n") if not line.strip().startswith("#"))
    assert "get_settings()" not in code


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
        s = Settings.model_validate(
            {
                "LANCE_REST_IMPL": "dir",
                "LANCE_S3_ACCESS_KEY_ID": "k",
                "LANCE_S3_SECRET_ACCESS_KEY": "s",
                "LANCE_WAREHOUSES_ENABLED": False,
            }
        )
        seen["settings"] = s
        return s

    app.dependency_overrides[deps.get_settings] = _settings
    app.state.namespace = object()

    response = TestClient(app).get("/probe/thing")
    assert response.status_code == 200, response.text
    assert seen, "the override was never consulted — `get_namespace` read the process settings instead, which is exactly the seam this finding is about"
