"""The lance-plane entrypoints are assembled once (docs/DECISIONS.md "The Python estate audit" DUP-12).

DUP-12: "The lance-service entrypoint is hand-assembled eight times; there is no
`make_lance_service_app`." The eight were catalog, lineage, the medallion producer, the medallion
mover, maintenance, viewer, search and annotator — each opening `app = FastAPI(...)` at module level
and then repeating the same five steps in prose comments copied between them.

The media three moved first, onto `service_kit.media.app.build_media_app` (see
`test_one_media_service_seam.py`). This pins the other five onto `service_kit.lance_app`.

THE DRIFT THE COPIES HAD ALREADY PRODUCED, and what makes this more than tidying: the medallion MOVER
served no `RequestIDMiddleware` at all. Its four siblings each added one, under the same copied
five-line comment about why a caller must be able to quote an id from a failed request — and the one
service that consumes the cascade's bus deliveries did not have it. Nothing compared the five.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import UnauthenticatedError


REPO = Path(__file__).resolve().parents[2]

#: The five lance-plane entrypoints. `services/gateway` is a proxy that builds its own app and is not
#: on this plane; the media trio builds through `service_kit.media.app`.
LANCE_MAINS = (
    "services/catalog/src/catalog/main.py",
    "services/lineage/src/lineage/main.py",
    "services/medallion/src/medallion/producer.py",
    "services/medallion/src/medallion/mover.py",
    "services/maintenance/src/maintenance/service.py",
)

LANCE_APPS = (
    ("catalog", "catalog.main"),
    ("lineage", "lineage.main"),
    ("medallion-producer", "medallion.producer"),
    ("medallion-mover", "medallion.mover"),
    ("maintenance", "maintenance.service"),
)


@pytest.fixture(autouse=True)
def _catalog_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`catalog.main` builds its settings at import, and the access key id has no default."""
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")


@pytest.mark.parametrize("main", LANCE_MAINS)
def test_no_lance_main_constructs_its_own_app(main: str) -> None:
    """RED before the collapse: all five carried a module-level `app = FastAPI(`."""
    source = (REPO / main).read_text()
    assert not re.search(r"^app = FastAPI\(", source, re.MULTILINE), f"{main} still hand-assembles its own FastAPI app"


@contextmanager
def _probe_route(app: FastAPI) -> Iterator[None]:
    """Add a probe route to a REAL, module-level app and take it away again.

    These apps are process-wide singletons — `catalog.main.app` is imported by every test that touches
    the catalog — so a route added here and left behind changes what
    `tests/unit/test_openapi_contract.py` sees the live app serve, and that test then fails for a
    reason that has nothing to do with it. Testing the real app is the point (a throwaway app built
    the same way proves only that the builder works), so the route goes on and comes off.
    """
    before = list(app.router.routes)
    schema = app.openapi_schema
    try:
        yield
    finally:
        app.router.routes[:] = before
        app.openapi_schema = schema


def _app(module_path: str) -> FastAPI:
    module = __import__(module_path, fromlist=["app"])
    app = module.app
    assert isinstance(app, FastAPI)
    return app


@pytest.mark.parametrize(("name", "module_path"), LANCE_APPS, ids=[row[0] for row in LANCE_APPS])
def test_every_lance_app_stamps_one_request_id(name: str, module_path: str) -> None:
    """THE DRIFT: the medallion mover served no request-id layer at all.

    Driven through a REQUEST rather than by looking for the class on `app.user_middleware`: a
    middleware that is registered but never reached stamps nothing, and the header is the contract.
    """
    app = _app(module_path)
    with _probe_route(app):

        @app.get("/_probe_request_id")
        async def _ok() -> dict[str, str]:
            return {"ok": "yes"}

        response = TestClient(app, raise_server_exceptions=False).get("/_probe_request_id", headers={"X-Request-ID": "quoted-by-a-caller"})
    assert response.headers.get("X-Request-ID") == "quoted-by-a-caller", dict(response.headers)


@pytest.mark.parametrize(("name", "module_path"), LANCE_APPS, ids=[row[0] for row in LANCE_APPS])
def test_every_lance_app_translates_a_lance_error(name: str, module_path: str) -> None:
    """The handler pair, in the order that makes it work — one place rather than five copied blocks."""
    app = _app(module_path)
    with _probe_route(app):

        @app.get("/_probe_unauthenticated")
        async def _raise() -> None:
            raise UnauthenticatedError("expired token")

        response = TestClient(app, raise_server_exceptions=False).get("/_probe_unauthenticated")
    assert response.status_code == 401, response.text
    assert response.headers["content-type"].startswith("application/problem+json"), response.headers


def test_the_catalog_maintenance_gate_stays_inside_the_request_id_layer() -> None:
    """Middleware added LATER is OUTER, so the read-only gate must be registered BEFORE the id layer.

    Registered after it, a maintenance-mode refusal never passes back through `RequestIDMiddleware`
    and goes out with no `X-Request-ID` — a 503 an operator cannot correlate to anything. This is the
    one reason `build_lance_service_app` takes `inner_middleware` at all, so it is pinned.
    """
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    app = _app("catalog.main")
    client = TestClient(app, raise_server_exceptions=False)
    # The gate reads the setting per request, so flipping it needs no rebuild — and it is put BACK,
    # because `app` is the process-wide catalog app every other test in this run also imports.
    original = getattr(app.state, "settings", None)
    app.state.settings = get_settings().model_copy(update={"maintenance_read_only": True})
    try:
        response = client.post("/v1/namespace/x/create", json={})
    finally:
        app.state.settings = original
    assert response.status_code == 503, response.text
    assert response.headers.get("X-Request-ID"), dict(response.headers)
