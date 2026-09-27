"""LH-144: an undrop announces that each table it re-registers exists again.

A recoverable drop emits `drop_table`, and lineage stamps the dataset DROP from it; the reconcile then
skips it, because absence on storage is the expected state of a dropped table. The undrop re-registers
the bytes, and without a CREATE fact of its own the table stayed dropped for the sweep: a live, governed
table with no back-fill, no loss detection and no freshness check, and nothing saying so. The live
catalog runs a 7-day grace period, so every drop there is recoverable. Both undrop doors now send the
same versionless `register_table` marker the register door sends, at the location the trash record kept.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
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
from catalog.api.v1.endpoints import namespaces as namespace_doors
from catalog.api.v1.endpoints import tables as table_doors
from catalog.services.dataplane import create_table
from service_kit.lakehouse.ns_errors import install_problem_handlers


pytest.importorskip("lance")

_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
NS = "zone"
TABLES = (["zone", "alpha"], ["zone", "beta"])


def _returning(value: object) -> Callable[[], object]:
    """An override with no parameters: FastAPI would read a default argument as a query parameter."""
    return lambda: value


@pytest.fixture
def emitted() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def client(tmp_path: Path, emitted: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    ns = connect("dir", {"root": str(tmp_path / "data")})
    ns.create_namespace(CreateNamespaceRequest(id=[NS]))
    for table_id in TABLES:
        create_table(ns, {}, table_id, pa.table({"id": pa.array([1, 2, 3], pa.int64())}), mode="create")

    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    for door in (namespace_doors, table_doors):
        application.include_router(door.router)
        application.include_router(door.management_router)
    control = tmp_path / "control"
    control.mkdir()
    settings = SimpleNamespace(
        delimiter="$", registry_root=str(control), storage_options=lambda: {}, trash_grace_days=7, fga_enabled=False, warehouses_enabled=False
    )
    for dependency, value in (
        (SettingsDep, settings),
        (NamespaceDep, ns),
        (CurrentToken, SimpleNamespace(sub=_SUB)),
        (FgaClientDep, None),
        (ControlEmitterDep, None),
    ):
        application.dependency_overrides[dependency.__metadata__[0].dependency] = _returning(value)

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _record(_emitter: Any, segments: list[str], **kwargs: Any) -> None:
        emitted.append({"segments": list(segments), **kwargs})

    for door in (namespace_doors, table_doors):
        monkeypatch.setattr(door, "emit_control", _noop)
        monkeypatch.setattr(door, "emit_write_event", _record)
    with TestClient(application) as test_client:
        yield test_client


def _registered(emitted: list[dict[str, Any]]) -> dict[str, str | None]:
    return {"$".join(call["segments"]): call.get("source_uri") for call in emitted if str(call.get("operation", "")).lower() == "register_table"}


def test_a_table_undrop_announces_the_table_exists_again(client: TestClient, emitted: list[dict[str, Any]]) -> None:
    assert client.post("/v1/table/zone$alpha/drop", json={}).status_code == 200, "the recoverable drop failed"
    emitted.clear()

    assert client.post("/management/v1/table/zone$alpha/undrop", json={}).status_code == 200

    registered = _registered(emitted)
    assert list(registered) == ["zone$alpha"], f"the undrop announced {emitted}"
    assert (registered["zone$alpha"] or "").endswith("zone$alpha"), "the marker must carry the location the bytes are at"


def test_a_namespace_undrop_announces_every_table_it_re_registers(client: TestClient, emitted: list[dict[str, Any]]) -> None:
    assert client.post("/v1/namespace/zone/drop", json={"behavior": "Cascade"}).status_code == 200, "the recoverable cascade failed"
    emitted.clear()

    assert client.post("/management/v1/namespace/zone/undrop", json={}).status_code == 200

    assert sorted(_registered(emitted)) == ["zone$alpha", "zone$beta"], f"the undrop announced {emitted}"
