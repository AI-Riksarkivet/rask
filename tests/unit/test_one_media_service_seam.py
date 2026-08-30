"""The media trio is assembled ONCE (open_python-audit DUP-16 + X12 + DUP-20).

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

import re
from collections.abc import Iterable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import UnauthenticatedError


REPO = Path(__file__).resolve().parents[2]
MEDIA_MAINS = (
    "services/viewer/src/viewer/main.py",
    "services/search/src/search/main.py",
    "services/annotator/src/annotator/main.py",
)


def test_service_kit_exports_one_register_middleware_name() -> None:
    """DUP-20: two different functions may not answer to one name.

    RED before the rename: `service_kit/middleware.py:register_middleware` (fleet: CORS with
    credentials + Timing) and `service_kit/media/middleware.py:register_middleware` (media: CORS with
    Range `expose_headers`, no Timing) had identical names AND identical signatures.
    """
    definitions = sorted(
        str(path.relative_to(REPO))
        for path in (REPO / "packages/service-kit/src").rglob("*.py")
        if re.search(r"^def register_middleware\(", path.read_text(), re.MULTILINE)
    )
    assert definitions == ["packages/service-kit/src/service_kit/middleware.py"], definitions


@pytest.mark.parametrize("main", MEDIA_MAINS)
def test_no_media_main_hand_rolls_the_lifespan_body(main: str) -> None:
    """DUP-16: the copied lifespan body lives in `service_kit.media`, not three times in three mains.

    RED before the collapse: all three carried `AppState(settings=settings, http=httpx.Client())`
    followed by the threadpooled `dataset_handle` open under the same comment.
    """
    source = (REPO / main).read_text()
    assert "AppState(settings=" not in source, f"{main} still builds the shared media AppState itself"
    assert "dataset_handle" not in source, f"{main} still opens the default dataset handle itself"
    assert "arm_drain_on_sigterm" not in source, f"{main} still arms the drain itself"


def test_the_media_lifespan_has_exactly_one_implementation() -> None:
    """The seam it collapses onto is a single module in `service_kit.media`."""
    splice = re.compile(r"AppState\(settings=")
    offenders = sorted(
        str(path.relative_to(REPO))
        for root in ("services", "packages")
        for path in (REPO / root).rglob("*.py")
        if "/tests/" not in str(path) and splice.search(path.read_text())
    )
    assert offenders == ["packages/service-kit/src/service_kit/media/lifespan.py"], offenders


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


def _paths(app: FastAPI) -> set[str]:
    """Every served path, walking FastAPI 0.140's lazy `_IncludedRouter` placeholders."""
    found: set[str] = set()

    def walk(routes: Iterable[object]) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
            else:
                path = getattr(route, "path", None)
                if isinstance(path, str):
                    found.add(path)

    walk(app.routes)
    return found


@pytest.mark.parametrize("name", ["viewer", "search"])
def test_the_test_seam_app_serves_the_operational_probes(name: str) -> None:
    """X12: `/livez` + `/readyz` are part of the app under test, not something only prod grows."""
    paths = _paths(_seam_app(name))
    assert {"/livez", "/readyz"} <= paths, sorted(paths)
