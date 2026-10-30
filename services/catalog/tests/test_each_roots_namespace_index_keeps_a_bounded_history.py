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

import importlib.util
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import cast

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


@pytest.fixture
def failed_roots(monkeypatch: pytest.MonkeyPatch) -> Callable[[], dict[str, int]]:
    """The tick's ``catalog.manifest_reclaim.failed`` counter, built by its own module against an in-memory reader."""
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

    from catalog.api import manifest_reclaim as tick

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    spec = importlib.util.find_spec(tick.__name__)
    assert spec is not None and spec.loader is not None, "the tick's module has no loader to run it again with"
    measured = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(otel_metrics, "get_meter", lambda name, *_args, **_kwargs: provider.get_meter(name))
        spec.loader.exec_module(measured)
    monkeypatch.setattr(tick, "_failed", measured._failed)

    def counts() -> dict[str, int]:
        found: dict[str, int] = {}
        data = reader.get_metrics_data()
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != "catalog.manifest_reclaim.failed":
                        continue
                    # A counter only ever yields `NumberDataPoint`, the member of the reader's point union that has a value.
                    for point in cast(Sequence[NumberDataPoint], metric.data.data_points):
                        found[str((point.attributes or {}).get("lance.catalog.root"))] = int(point.value)
        return found

    return counts


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


def test_a_root_the_reclaim_cannot_read_is_counted_and_leaves_the_others(catalog: Catalog, failed_roots: Callable[[], dict[str, int]], tmp_path: Path) -> None:
    """A root whose index Lance cannot open fails alone: the tick still answers, reclaims every other root, and counts
    the failure under that root, which is what pages once the same root keeps failing."""
    from catalog.services import warehouses

    broken = tmp_path / "broken"
    (broken / "__manifest" / "_versions").mkdir(parents=True)
    (broken / "__manifest" / "_versions" / "1.manifest").write_bytes(b"not a Lance manifest")
    warehouses.put_warehouse(catalog.estate, {}, {"id": "broken", "bucket": "broken", "project": "p", "root_uri": str(broken)})
    tenant = connect("dir", {"root": catalog.warehouse})
    for name in ("one", "two"):
        tenant.create_namespace(CreateNamespaceRequest(id=[name]))

    tick = catalog.client.post(f"/{BINDING}")

    assert tick.status_code == 200, tick.text
    assert (tick.json()["failed"], [done["root"] for done in tick.json()["reclaimed"]], failed_roots()) == (
        1,
        [catalog.warehouse],
        {str(broken): 1},
    )


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
