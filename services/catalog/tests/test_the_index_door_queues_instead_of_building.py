"""`create_index` answers with a unit id; the build happens elsewhere — clause 4, the index half.

Building an index inside the request handler makes the cost of a request a property of the TABLE. No
pod sizing bounds it, because the next table is bigger — the same defect the `maintenance/compact`
door had before it became a 202.

**The spec asks for the queued form.** `CreateTableIndex` states that "index creation is handled
asynchronously" and points a caller at `ListTableIndices` / `DescribeTableIndexStats` for progress;
its response carries an optional `transaction_id` and nothing else. So answering with a unit id is
spec-conformant and the synchronous build was the divergence — which is why this needs no new
response shape and no 202.

Driven through a real ASGI client rather than by calling the handler, because what is asserted is
what an operator receives: the status, the body's `transaction_id`, and the fact that no index
appeared on the dataset by the time the response did.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, DeclareTableRequest, connect

from catalog.api.dependencies import LineageEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import indices
from service_kit.lakehouse.ns_errors import install_problem_handlers


class _Publisher:
    """Records what reached the broker. A double rather than a sidecar: what is under test is the
    door's decision to publish and the unit it builds, not Dapr's delivery."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []


@pytest.fixture
def dataset(tmp_path: Path) -> tuple[Any, str]:
    ns = connect("dir", {"root": str(tmp_path)})
    ns.create_namespace(CreateNamespaceRequest(id=["ns1"]))
    location = ns.declare_table(DeclareTableRequest(id=["ns1", "t"])).location
    lance.write_dataset(pa.table({"id": pa.array(range(64), pa.int64())}), location)
    return ns, location


def _app(ns: Any, *, index_topic: str, publisher: _Publisher | None, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(indices.router)
    application.state.dapr_client = publisher

    settings = SimpleNamespace(
        delimiter="$",
        maintenance_index_topic=index_topic,
        maintenance_index_pubsub="maintenance-index-pubsub",
        control_emit_timeout_seconds=5,
    )
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: ns
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: SimpleNamespace(sub="alice")
    application.dependency_overrides[LineageEmitterDep.__metadata__[0].dependency] = lambda: None

    async def _publish(client: Any, **kwargs: Any) -> None:
        assert publisher is not None
        publisher.published.append(kwargs)

    monkeypatch.setattr(indices.dapr_publish, "publish_event", _publish)

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(indices.lineage_deps, "emit_measured_write", _noop)
    return application


@pytest.fixture
def queued(dataset: tuple[Any, str], monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _Publisher, str]]:
    ns, location = dataset
    publisher = _Publisher()
    with TestClient(_app(ns, index_topic="maintenance.index.v1", publisher=publisher, monkeypatch=monkeypatch)) as client:
        yield client, publisher, location


def test_the_door_returns_a_unit_id_and_builds_NOTHING(queued: tuple[TestClient, _Publisher, str]) -> None:
    """The headline: the response arrives before any index does."""
    client, publisher, location = queued

    response = client.post("/v1/table/ns1$t/create_scalar_index", json={"column": "id", "index_type": "BTREE"})

    assert response.status_code == 200, response.text
    assert response.json()["transaction_id"].startswith("index-"), response.text
    assert lance.dataset(location).describe_indices() == [], "the handler built the index it was supposed to queue"
    assert len(publisher.published) == 1


def test_the_unit_carries_everything_the_worker_needs(queued: tuple[TestClient, _Publisher, str]) -> None:
    """A unit crossing a broker must be self-contained — the rule `DatasetWorkItem` already states.

    The LOCATION is asked of the catalog rather than composed from settings (rule I2): the two
    disagree for most of this estate, and a unit carrying the wrong one indexes another table. The
    table ID rides too, because without it the worker signs the write with its ambient credential
    instead of one scoped to this table.
    """
    client, publisher, location = queued

    client.post("/v1/table/ns1$t/create_scalar_index", json={"column": "id", "index_type": "BTREE", "name": "id_idx"})

    unit = json.loads(publisher.published[0]["data"])
    assert unit["uri"] == location
    assert unit["table_id"] == "ns1$t"
    assert (unit["column"], unit["index_type"], unit["name"], unit["kind"]) == ("id", "BTREE", "id_idx", "scalar")


def test_the_two_doors_stamp_DIFFERENT_kinds(queued: tuple[TestClient, _Publisher, str]) -> None:
    """Vector and scalar are separate spec operations, so which one was asked for is the CALLER's
    statement. A worker inferring it from `index_type` would build the wrong index for a table."""
    client, publisher, _ = queued

    client.post("/v1/table/ns1$t/create_scalar_index", json={"column": "id", "index_type": "BTREE"})
    client.post("/v1/table/ns1$t/create_index", json={"column": "id", "index_type": "IVF_PQ"})

    kinds = [json.loads(p["data"])["kind"] for p in publisher.published]
    assert kinds == ["scalar", "vector"]


def test_the_spec_field_names_are_translated_to_pylances(queued: tuple[TestClient, _Publisher, str]) -> None:
    """The spec says `distance_type`; pylance's keyword is `metric`. The catalog is the party that
    speaks both, so it translates — a worker doing it would need a namespace handle pointed at the
    catalog's own root, making a dataset-level service a second writer to `__manifest`.

    UNSET fields are OMITTED rather than sent as `None`: pylance's defaults are meaningful, and an
    explicit `None` would override them with something no caller asked for.
    """
    client, publisher, _ = queued

    client.post("/v1/table/ns1$t/create_index", json={"column": "id", "index_type": "IVF_PQ", "distance_type": "cosine"})

    params = json.loads(publisher.published[0]["data"])["params"]
    assert params == {"metric": "cosine"}, params


def test_WITHOUT_a_topic_the_build_still_happens_here(dataset: tuple[Any, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing would ever execute a unit nobody consumes, so a deployment with no index queue must get
    the synchronous build it has always had. Both sides read the same topic name for that reason."""
    ns, location = dataset
    with TestClient(_app(ns, index_topic="", publisher=None, monkeypatch=monkeypatch)) as client:
        response = client.post("/v1/table/ns1$t/create_scalar_index", json={"column": "id", "index_type": "BTREE"})

    assert response.status_code == 200, response.text
    assert [i.name for i in lance.dataset(location).describe_indices()], "no queue and no index — the work vanished"
