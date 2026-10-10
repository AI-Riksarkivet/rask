"""The media trio is assembled ONCE (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-16 + X12 + DUP-20).

`viewer`, `search` and `annotator` are three deployments of one shape — a Lance media service over
`service_kit.media` — and each hand-assembled that shape in its own `main.py`. Three findings live on
that one surface:

* **DUP-16** — the lifespan body was copy-pasted three ways (build `AppState`, warm the default
  dataset handle off the loop, `attach_auth`, set the lifecycle flags, arm the SIGTERM drain, dispose
  in a `finally`) and the copies had already drifted: viewer disposed FGA before disarming the drain,
  search disarmed first, and the three closed different resource sets in different orders.
* **X12** — `create_viewer_app` / `create_search_app`, the declared test/composition seam, built a
  DIFFERENT app from production: no problem handlers on search's, no probes on either, no body cap on
  search's. A regression in exactly the layer their production comments describe (an
  `UnauthenticatedError` answering 500 instead of 401) could not be caught through the seam.
* **DUP-20** — `service_kit` exported two different `register_middleware` functions under one name
  (`service_kit.middleware` and `service_kit.media.middleware`), imported bare by different callers,
  so a call site did not say which stack it registered.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import UnauthenticatedError


def _seam_app(name: str) -> FastAPI:
    if name == "viewer":
        from viewer.main import create_viewer_app

        return create_viewer_app()
    from search.main import create_search_app
    from service_kit.media.state import AppState

    return create_search_app(AppState())


@pytest.mark.parametrize("name", ["viewer", "search"])
def test_the_test_seam_app_maps_the_same_errors_production_does(name: str) -> None:
    """X12: an app built through the seam must translate the same exceptions the deployed one does.

    DRIVEN THROUGH A REQUEST, not by inspecting the handler table: starlette resolves a handler by
    MRO at dispatch, so "is `UnauthenticatedError` a key" answers the wrong question and would pass on
    an app that maps nothing. This raises the exact exception the OIDC verifier raises and reads the
    response.

    RED before the fix: `create_search_app` registered only `register_handlers`, which maps
    `DomainError` alone — so an expired or wrong-audience bearer came back `500 text/plain` from
    starlette's fallback, the very regression both mains' production comments are about.
    """
    app = _seam_app(name)

    @app.get("/_probe_unauthenticated")
    async def _raise() -> None:
        raise UnauthenticatedError("expired token")

    response = TestClient(app, raise_server_exceptions=False).get("/_probe_unauthenticated")
    assert response.status_code == 401, response.text
    assert response.headers["content-type"].startswith("application/problem+json"), response.headers
    assert response.json()["title"] == "UnauthenticatedError", response.json()
