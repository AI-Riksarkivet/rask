"""Each root's namespace index keeps a bounded history, and the catalog still reads its latest ([[LH-251]]).

A ``dir`` root keeps its namespaces and tables as rows of the ``__manifest`` Lance table
(``lance_docs/ns_catalog/catalog/dir/index.md`` § V2: Manifest), and every namespace operation rewrites it as a new
version that nothing in the backend reclaims (measured on pylance 13.0.0: one version per operation, a dropped id
readable at latest - 1). The catalog, its only writer, reclaims each root's history on its own cron tick with Lance's
``cleanup_old_versions``, and a connection it holds is never older than that history's retention allows, because the
backend reloads a held ``__manifest`` by probing only the version after it (lance v13.0.0
``rust/lance-namespace-impls/src/dir/manifest.rs``, ``DatasetConsistencyWrapper``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import lance
import pytest
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, DropNamespaceRequest, connect
from pydantic import BaseModel, ConfigDict


BINDING = "catalog-manifest-reclaim-cron"


class Catalog(BaseModel):
    """The catalog app over a ``dir`` estate root, and a registered warehouse's root beside it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: TestClient
    estate: str
    warehouse: str


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Catalog]:
    """The catalog with its own namespace connections, keeping no ``__manifest`` version but the latest."""
    estate, warehouse = tmp_path / "estate", tmp_path / "warehouse"
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(estate),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_MANIFEST_RETAIN_SECONDS": "0",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.api.manifest_reclaim import mount_manifest_reclaim
    from catalog.core.config import get_settings
    from catalog.services import warehouses

    get_settings.cache_clear()
    from catalog.main import app

    routes = list(app.router.routes)
    mount_manifest_reclaim(app, BINDING)
    warehouses.put_warehouse(str(estate), {}, {"id": "wh", "bucket": "wh", "project": "p", "root_uri": str(warehouse)})
    with TestClient(app) as client:
        yield Catalog(client=client, estate=str(estate), warehouse=str(warehouse))
    app.router.routes[:] = routes
    get_settings.cache_clear()


def _object_ids(root: str, version: int) -> list[str]:
    return lance.dataset(f"{root}/__manifest", version=version).to_table(columns=["object_id"]).column(0).to_pylist()


def _versions(root: str) -> list[int]:
    return [int(v["version"]) for v in lance.dataset(f"{root}/__manifest").versions()]


def test_the_reclaim_keeps_each_roots_history_bounded_and_the_catalog_reads_the_latest(catalog: Catalog) -> None:
    """Every root keeps only its retained versions, no retained version holds a dropped id, and a connection the
    catalog built before another writer moved its root on still lists what that writer made."""
    assert catalog.client.post("/v1/namespace/kept/create", json={}).status_code == 200
    replica = connect("dir", {"root": catalog.estate})
    replica.create_namespace(CreateNamespaceRequest(id=["other"]))
    replica.create_namespace(CreateNamespaceRequest(id=["gone"]))
    replica.drop_namespace(DropNamespaceRequest(id=["gone"]))
    tenant = connect("dir", {"root": catalog.warehouse})
    for name in ("w_kept", "w_gone"):
        tenant.create_namespace(CreateNamespaceRequest(id=[name]))
    tenant.drop_namespace(DropNamespaceRequest(id=["w_gone"]))

    tick = catalog.client.post(f"/{BINDING}")

    assert tick.status_code == 200, tick.text
    assert {root: len(_versions(root)) for root in (catalog.estate, catalog.warehouse)} == {catalog.estate: 1, catalog.warehouse: 1}
    assert [v for v in _versions(catalog.estate) if "gone" in _object_ids(catalog.estate, v)] == []
    assert [v for v in _versions(catalog.warehouse) if "w_gone" in _object_ids(catalog.warehouse, v)] == []
    listed = catalog.client.get("/v1/namespace/$/list")
    assert listed.status_code == 200, listed.text
    assert sorted(listed.json()["namespaces"]) == ["kept", "other"]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("age", "built_again"),
    [pytest.param(99.999, False, id="younger-than-a-quarter"), pytest.param(100.0, True, id="a-quarter-of-the-retention")],
)
def test_a_connection_is_built_again_once_a_quarter_of_the_retention_old(
    age: float, built_again: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 400 s of retention a connection is served for 100 s, and three quarters of the retention stay between the
    last read a served connection made and any version the reclaim removes: the margin for clocks that disagree."""
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_MANIFEST_RETAIN_SECONDS": "400",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import Settings
    from catalog.core.namespace import NamespaceConnections

    settings, clock = Settings(), _Clock()
    connections = NamespaceConnections(clock=clock)
    first = connections.connection(settings)
    clock.now = age

    served = connections.connection(settings)

    assert (served is not first) is built_again


def test_the_namespace_index_is_written_without_indexes_of_its_own(catalog: Catalog) -> None:
    """The catalog's connections leave ``inline_optimization_enabled`` off: measured over moto at 100 and 1,000
    objects, the three spec indexes made a namespace create 16 store requests instead of 12 and a describe 6 instead
    of 3, since the unindexed index is one data file a read takes in one GET."""
    assert catalog.client.post("/v1/namespace/one/create", json={}).status_code == 200

    indexes = lance.dataset(f"{catalog.estate}/__manifest").describe_indices()

    assert indexes == []


def test_a_commit_path_cleanup_policy_on_the_index_leaves_its_history(catalog: Catalog) -> None:
    """``lance.auto_cleanup.*`` on ``__manifest`` bounds nothing: the backend's rewrite commits skip it
    (``skip_auto_cleanup``), so the reclaim tick is what bounds the index, and a policy armed there is not a fix."""
    assert catalog.client.post("/v1/namespace/seed/create", json={}).status_code == 200
    lance.dataset(f"{catalog.estate}/__manifest").update_config({"lance.auto_cleanup.interval": "1", "lance.auto_cleanup.older_than": "0s"})
    before = len(_versions(catalog.estate))

    for name in ("a", "b", "c"):
        assert catalog.client.post(f"/v1/namespace/{name}/create", json={}).status_code == 200

    assert len(_versions(catalog.estate)) == before + 3
