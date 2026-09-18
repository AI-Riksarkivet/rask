"""A CASCADE drop records the deletion of every table it destroys, as the table door does.

[[LH-144]]. The single-table door emits a `drop_table` lineage run — its own comment says why: "the
dataset node persists in the graph, named a `drop_table` run". A CASCADE destroys its children inside
one native call, and those children never reach that door, so nothing recorded their deletion.

WHAT THAT COSTS IS A GRAPH THAT NEVER FORGETS. `repository.dropped_at` derives from run history — the
most recent SUCCESSFUL run being a `drop_table` — never a stored flag. A table with no such run stays
indistinguishable from a live one, so `lineage_reconcile_ungoverned` names it on every tick, forever,
for bytes that no longer exist. Measured on the live estate 2026-09-18: 20 datasets reported
ungoverned, 8 of them tables this repo's own suites had cascade-dropped hours earlier.

It is also condition 1 read backwards. A write's provenance is supposed to survive it; a DELETE is a
write, and a cascade's deletions were leaving none.

EMITTED BEFORE THE REVOKE, the same ordering the table door states and for the same reason: on the
http transport the caller's bearer authorizes ingest against their still-live write grant, so
revoking first would 403 the very event that records who dropped the table.
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
from lance_namespace import connect
from lance_namespace_urllib3_client.models.create_namespace_request import CreateNamespaceRequest

from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import namespaces as door
from catalog.services.dataplane import create_table
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
NS = "zone"
TABLES = (["zone", "alpha"], ["zone", "beta"])


def _ipc() -> bytes:
    table = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, a runtime-only type
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    namespace.create_namespace(CreateNamespaceRequest(id=[NS]))
    for table_id in TABLES:
        create_table(namespace, {}, table_id, _ipc(), mode="create")
    return namespace


@pytest.fixture
def emitted() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def app(ns, emitted: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:  # noqa: ANN001
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)

    control = tmp_path / "control"
    control.mkdir()
    settings = SimpleNamespace(
        delimiter="$",
        registry_root=str(control),
        storage_options=lambda: {},
        trash_grace_days=0,
        fga_enabled=False,
    )
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: ns
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: SimpleNamespace(sub=_SUB)
    application.dependency_overrides[FgaClientDep.__metadata__[0].dependency] = lambda: None
    application.dependency_overrides[ControlEmitterDep.__metadata__[0].dependency] = lambda: None

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _record(_emitter: Any, segments: list[str], **kwargs: Any) -> None:
        emitted.append({"segments": list(segments), **kwargs})

    monkeypatch.setattr(door, "emit_control", _noop)
    # `raising=False` so the RED run reports the MISSING EMIT rather than an AttributeError about the
    # patch target — the assertion is the message worth reading.
    monkeypatch.setattr(door, "emit_write_event", _record, raising=False)
    yield application


def _dropped(emitted: list[dict[str, Any]]) -> set[str]:
    return {"$".join(call["segments"]) for call in emitted if str(call.get("operation", "")).lower().endswith("drop_table")}


def test_a_cascade_records_a_drop_for_every_table_it_destroys(app: FastAPI, emitted: list[dict[str, Any]]) -> None:
    with TestClient(app) as client:
        response = client.post(f"/v1/namespace/{NS}/drop", json={"id": [NS], "behavior": "CASCADE"})

    assert response.status_code == 200, response.text
    assert _dropped(emitted) == {"zone$alpha", "zone$beta"}, (
        "the cascade destroyed tables without recording their deletion, so `dropped_at` derives nothing "
        f"and the reconcile reports them ungoverned forever: {emitted}"
    )


def test_a_RESTRICT_drop_records_nothing_because_it_destroys_nothing(app: FastAPI, ns, emitted: list[dict[str, Any]]) -> None:  # noqa: ANN001
    """The control. A non-cascade drop of a non-empty namespace is refused, so there is no deletion to
    record — and a door that emitted regardless would mark live tables dropped."""
    with TestClient(app) as client:
        client.post(f"/v1/namespace/{NS}/drop", json={"id": [NS], "behavior": "RESTRICT"})

    assert _dropped(emitted) == set(), f"a refused drop recorded deletions for tables that still exist: {emitted}"
