"""A governed tier's row is the writer's, and the one platform column it may carry is held to its width (ADR 0156 C9).

Provenance lives in the commit ([[LH-225]]): the run and inputs a silver or gold write names are its transaction
properties, so no column is required of a governed row. `stage` and `lineage` are the writer's own columns.
`source_rowid` is an opt-in a pipeline writes to name the bronze row a row descends from; a table that carries it
must carry it as `uint64`, the width of Lance's stable row id, or it is a different column wearing the right name:
present, non-null, and passing every count-based check.

Driven on the wire against a real `dir` namespace and real pylance writes: the subject is what publish does to a
real dataset.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, DescribeTableRequest, LanceNamespace, connect

from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep, StorageOptionsDep, get_lineage_emitter
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import publication as door
from catalog.core.identifiers import with_minted_uuid
from catalog.core.lineage_emit import NoopEmitter
from catalog.services.dataplane import create_table
from catalog.services.publication import published_version
from service_kit.governed.oidc import IDToken
from service_kit.governed.person_identity import PersonPrincipal
from service_kit.governed.principal import Principal
from service_kit.lakehouse.base_registry import BaseRegistry
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

#: A person the dev Dex's password connector reports: `sub` is Dex's packing of its user id with the connector id,
#: and the principal is the user id under the idp id `local` maps to.
_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
_CALLER = PersonPrincipal(
    principal=Principal(idp_id="dex", subject="08a8684b-db88-4b73-90a9-3cd1661f5466"),
    claims=IDToken(iss="https://dex", sub=_SUB, aud="lance", exp=1, iat=1),
)
TABLE_ID = ["db", "features"]


def _rows(source_rowid: pa.DataType) -> pa.Table:
    """A silver row with no `stage` or `lineage` column, carrying `source_rowid` at ``source_rowid``'s width."""
    return pa.table({"id": pa.array([1, 2, 3], pa.int64()), "payload": pa.array(["p1", "p2", "p3"]), "source_rowid": pa.array([11, 12, 13], source_rowid)})


def _app(namespace: Any, monkeypatch: pytest.MonkeyPatch, registry_root: str) -> FastAPI:
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    settings = SimpleNamespace(registry_root=registry_root, storage_options=lambda: {})
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: namespace
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: _CALLER
    application.dependency_overrides[FgaClientDep.__metadata__[0].dependency] = lambda: object()
    application.dependency_overrides[ControlEmitterDep.__metadata__[0].dependency] = lambda: None
    application.dependency_overrides[get_lineage_emitter] = _Tenant

    async def _noop_emit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(door, "emit_control", _noop_emit)
    return application


class _Tenant(NoopEmitter):
    """No lineage transport, and the tenant every table here belongs to."""

    async def project_for(self, top_ns: str) -> str | None:
        del top_ns
        return "acme"


@pytest.fixture
def registry_root(tmp_path: Path) -> str:
    root = tmp_path / "control"
    root.mkdir()
    return str(root)


def _client_over(payload: pa.Table, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_root: str) -> Iterator[tuple[TestClient, LanceNamespace]]:
    root = str(tmp_path / "data")
    namespace = connect("dir", {"root": root})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1], properties=with_minted_uuid(None)))
    registry = BaseRegistry(control_root=registry_root, storage_options={})
    create_table(namespace, {}, TABLE_ID, payload, root=root, mode="create", registry=registry, holder="db$features")
    with TestClient(_app(namespace, monkeypatch, registry_root)) as c:
        yield c, namespace


def _publish(client: TestClient, **body: Any) -> Any:  # noqa: ANN401
    return client.post("/management/v1/table/db$features/publish", json={"version": 1, **body})


def test_a_tier_without_stage_or_lineage_publishes_and_keeps_its_source_rowid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_root: str) -> None:
    client, namespace = next(_client_over(_rows(pa.uint64()), tmp_path, monkeypatch, registry_root))

    response = _publish(client, key_column="id")

    assert (response.status_code, response.json().get("published")) == (200, True), response.text
    location = namespace.describe_table(DescribeTableRequest(id=TABLE_ID)).location
    published = lance.dataset(location, version=published_version(namespace, {}, TABLE_ID))
    assert published.to_table(columns=["source_rowid"]).column(0).to_pylist() == [11, 12, 13]


def test_a_source_rowid_of_the_wrong_width_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_root: str) -> None:
    client, namespace = next(_client_over(_rows(pa.int64()), tmp_path, monkeypatch, registry_root))

    response = _publish(client, key_column="id")

    assert (response.status_code, "uint64" in response.text) == (400, True), response.text
    assert published_version(namespace, {}, TABLE_ID) is None, "the refusal must be fail-closed — nothing may be pointed at"
