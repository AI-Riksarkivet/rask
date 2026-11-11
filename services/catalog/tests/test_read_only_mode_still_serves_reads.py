"""Read-only maintenance mode must refuse WRITES, not the entire read surface, and no name may read as a read.

The Lance Namespace route grammar is `POST /v1/<object>/{id}/<action>`: the spec puts the ACTION in the path, not in the
verb, so `describe`, `exists`, `count_rows`, `query`, `stats`, `version/list`, `tags/list`, `index/list`,
`policy/describe` and `access/my-permissions` are all POSTs. A window that refused every POST would make the catalog
unreadable, which is the outage the window exists to avoid; so a POST is classified by the action of the ROUTE it
matches, from a READ allowlist. An action nobody has classified, and a request matching no route, are writes.

DELETE, PUT and PATCH are writes whatever they name. A path's last segment can be a name a caller chose
(`DELETE /management/v1/projects/list`), so the path text never classifies a request: only the route it matches does.

The catalog's cron ticks write too (an erasure release deletes tags and branches, the manifest reclaim deletes
versions), and Dapr delivers them at the pod root, outside both mounts. Through a window each tick is answered 200 and
does nothing, so Dapr counts no failed delivery and the next tick after the window does the work.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


#: Reads served as POST, on both mounts: the spec puts the action in the path, and rask's management doors
#: follow the same grammar. A read-tier vend is a read (a direct reader's way in); a write-tier vend is not.
READS = [
    ("POST", "/v1/table/pages/describe"),
    ("POST", "/v1/table/pages/count_rows"),
    ("POST", "/management/v1/table/pages/changes"),
    ("POST", "/management/v1/table/pages/credentials?tier=read"),
    ("POST", "/management/v1/access/list-objects"),
    ("POST", "/management/v1/access/expand"),
    ("GET", "/management/v1/projects"),
]

#: Writes on both mounts, which must keep being refused: the spec's `/v1` and rask's `/management/v1`
#: (LH-446), where erasure, policy, grants and the registries live.
WRITES = [
    ("POST", "/v1/table/pages/insert"),
    # A name holding `?` ([[LH-526]]): the write is classified by the path routing matched, not one cut at the `?`.
    ("POST", "/v1/namespace/describe%3F/create"),
    ("POST", "/management/v1/table/pages/erasure"),
    ("POST", "/management/v1/warehouses"),
    ("POST", "/management/v1/table/pages/credentials?tier=write"),
    # Fail-closed: an action nobody has classified, and a path no route matches, are writes.
    ("POST", "/v1/table/pages/some_future_operation"),
    # A DELETE of an object named like a read action, and like the vend door at the read tier.
    ("DELETE", "/management/v1/projects/list"),
    ("DELETE", "/management/v1/projects/credentials?tier=read"),
]


@pytest.fixture
def read_only_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """The real catalog app with `LANCE_MAINTENANCE_READ_ONLY=true`, its namespace a local `dir` root."""
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("LANCE_MAINTENANCE_READ_ONLY", "true")
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    get_settings.cache_clear()


@pytest.mark.parametrize(("method", "path"), READS)
def test_a_request_that_only_READS_survives_a_maintenance_window(read_only_catalog: TestClient, method: str, path: str) -> None:
    response = read_only_catalog.request(method, path, json={} if method == "POST" else None)
    assert response.status_code != 503, f"{method} {path} is a READ — refusing it makes the catalog unreadable, not read-only: {response.text}"


@pytest.mark.parametrize(("method", "path"), WRITES)
def test_a_request_that_WRITES_is_refused_through_the_window(read_only_catalog: TestClient, method: str, path: str) -> None:
    response = read_only_catalog.request(method, path, json={})
    assert (response.status_code, response.json().get("code")) == (503, 17), f"{method} {path} writes and must not pass a read-only window: {response.text}"
    assert response.headers.get("Retry-After"), dict(response.headers)


#: Every cron binding the catalog mounts, each at the name the chart gives its Component.
CRON_TICKS = {
    "LANCE_CONTROL_RELAY_BINDING_NAME": "catalog-control-relay-cron",
    "LANCE_ERASURE_RELEASE_BINDING_NAME": "catalog-erasure-release-cron",
    "LANCE_PROMOTION_EXPIRY_BINDING_NAME": "catalog-promotion-expiry-cron",
    "LANCE_COMMIT_RECONCILE_BINDING_NAME": "catalog-commit-reconcile-cron",
    "LANCE_MANIFEST_RECLAIM_BINDING_NAME": "catalog-manifest-reclaim-cron",
}


@pytest.fixture
def read_only_catalog_with_its_ticks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """The real catalog app in a window, with every cron tick mounted: the ticks are mounted at import from the binding
    names, so the module is reloaded with them, and reloaded again without them once the test is done."""
    import catalog.main
    from catalog.core.config import get_settings

    for name, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_MAINTENANCE_READ_ONLY": "true",
        "APP_API_TOKEN": "sidecar",
        **CRON_TICKS,
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        with TestClient(importlib.reload(catalog.main).app, raise_server_exceptions=False) as client:
            yield client
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        importlib.reload(catalog.main)
        get_settings.cache_clear()


@pytest.mark.parametrize("binding", list(CRON_TICKS.values()))
def test_a_cron_tick_does_nothing_through_the_window(read_only_catalog_with_its_ticks: TestClient, binding: str) -> None:
    tick = read_only_catalog_with_its_ticks.post(f"/{binding}", headers={"dapr-api-token": "sidecar"})
    assert (tick.status_code, tick.json()) == (200, {"skipped": True}), tick.text
