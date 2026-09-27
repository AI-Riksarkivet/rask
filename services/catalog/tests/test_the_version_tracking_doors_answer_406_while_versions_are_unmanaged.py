"""CreateTableVersion, BatchCreateTableVersions and BatchCommitTables answer 406 while this catalog does
not manage table versions.

[[LH-206]]. The spec pairs these ops with `managed_versioning` (`lance_docs/ns_catalog/spec.yaml`, the
`DescribeTableResponse.managed_versioning` field): a caller is to route commits through them only when
`describe_table` says so. rask never does, so a stock client commits through Lance's own manifest CAS
and nothing in the estate calls these doors. Served, `version/create` would publish a client-staged
manifest the file-version refusal never judges, and the batch doors would run sub-operations without
the protection, ownership and lineage their own doors carry.

The refusal is the catalog's, not the backend's: a backend that implements the op is not called.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import (
    BatchCommitTablesResponse,
    BatchCreateTableVersionsResponse,
    CreateTableRequest,
    DescribeTableRequest,
    LanceNamespace,
    connect,
)

from catalog.api import fga_deps, lineage_deps
from catalog.api.dependencies import FgaClientDep, LineageEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import versions as version_door
from catalog.core.config import Settings
from catalog.core.namespace import build_namespace
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")


def _inverted(version: int) -> int:
    """Lance names a version's manifest by the u64 complement."""
    return (1 << 64) - 1 - version


def _app(ns: object, monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, list[list[str]]]:
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(version_door.router)
    settings = SimpleNamespace(delimiter="$", storage_options=dict, warehouses_enabled=False, fga_enabled=False, root="")
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: ns
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: SimpleNamespace(sub="alice")
    application.dependency_overrides[LineageEmitterDep.__metadata__[0].dependency] = lambda: None
    application.dependency_overrides[FgaClientDep.__metadata__[0].dependency] = lambda: None
    seeded: list[list[str]] = []

    async def _record_seed(_client: object, _settings: object, _token: object, *, resource: str, segments: list[str]) -> None:
        seeded.append(list(segments))

    async def _no_emit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(fga_deps, "seed_ownership", _record_seed)
    monkeypatch.setattr(lineage_deps, "emit_measured_write", _no_emit)
    return application, seeded


def _unsupported(answer: Any) -> None:  # noqa: ANN401 — an httpx Response
    assert answer.status_code == 406, answer.text
    assert answer.json()["code"] == 0, f"not the spec's Unsupported code: {answer.json()}"
    assert "managed_versioning" in answer.json()["detail"]


@pytest.fixture
def staged(tmp_path: Path) -> tuple[LanceNamespace, Path, Path]:
    """`t` at version 1 with a real version-2 manifest Lance wrote, moved into the spec's staging shape."""
    root = tmp_path / "data"
    namespace = connect("dir", {"root": str(root)})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, pa.schema([pa.field("i", pa.int64())])) as writer:
        writer.write_table(pa.table({"i": pa.array([1, 2, 3], pa.int64())}))
    namespace.create_table(CreateTableRequest(id=["t"]), sink.getvalue().to_pybytes())
    table_dir = root / "t.lance"
    lance.write_dataset(pa.table({"i": pa.array([4], pa.int64())}), str(table_dir), mode="append")
    staging = table_dir / "_versions" / f"{_inverted(2)}.manifest-{uuid.uuid4()}"
    shutil.move(table_dir / "_versions" / f"{_inverted(2)}.manifest", staging)
    (table_dir / "_versions" / "latest_version_hint.json").write_text("1")
    return namespace, table_dir, staging


def test_version_create_answers_406_and_moves_no_manifest(staged: tuple[LanceNamespace, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    namespace, table_dir, staging = staged
    application, _ = _app(namespace, monkeypatch)

    with TestClient(application) as client:
        answer = client.post("/v1/table/t/version/create", json={"version": 2, "manifest_path": str(staging)})

    _unsupported(answer)
    assert staging.exists(), "the staged manifest was moved into the version slot"
    assert [v["version"] for v in lance.dataset(str(table_dir)).versions()] == [1]


def test_batch_create_answers_406_whatever_the_backend_implements(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MagicMock()
    backend.describe_table.return_value.location = "s3://bucket/t.lance"
    backend.batch_create_table_versions.return_value = BatchCreateTableVersionsResponse(versions=[])
    application, _ = _app(backend, monkeypatch)

    with TestClient(application) as client:
        answer = client.post("/v1/table/version/batch-create", json={"entries": [{"id": ["t"], "version": 2, "manifest_path": "t.lance/_versions/x"}]})

    _unsupported(answer)
    backend.batch_create_table_versions.assert_not_called()


def test_batch_commit_answers_406_and_creates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MagicMock()
    backend.batch_commit_tables.return_value = BatchCommitTablesResponse(results=[])
    application, seeded = _app(backend, monkeypatch)

    with TestClient(application) as client:
        answer = client.post("/v1/table/batch-commit", json={"operations": [{"declare_table": {"id": ["db1", "fresh"]}}]})

    _unsupported(answer)
    backend.batch_commit_tables.assert_not_called()
    assert seeded == [], "ownership was seeded for a table the refused batch never declared"


def test_the_catalog_namespace_does_not_advertise_managed_versioning(tmp_path: Path) -> None:
    """The precondition the three 406s rest on, read from the namespace the catalog itself builds."""
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_REST_ROOT=str(tmp_path / "data"))
    namespace = build_namespace(settings)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, pa.schema([pa.field("i", pa.int64())])) as writer:
        writer.write_table(pa.table({"i": pa.array([1], pa.int64())}))
    namespace.create_table(CreateTableRequest(id=["t"]), sink.getvalue().to_pybytes())

    described = namespace.describe_table(DescribeTableRequest(id=["t"]))

    assert described.managed_versioning is not True, "the catalog now manages table versions — the version-tracking doors must be built, not refused"
