"""BatchCreateTableVersions and BatchCommitTables answer 406: the catalog judges one table version at a time.

[[LH-206]], [[LH-471]]. A stock client commits each table through ``CreateTableVersion``, which the catalog judges
before the backend's put-if-not-exists (ADR 0157). A batch finalizes several tables as one, and its sub-operations
would run without the protection, ownership and lineage their own doors carry.

The refusal is the catalog's, not the backend's: a backend that implements the op is not called.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import (
    BatchCommitTablesResponse,
    BatchCreateTableVersionsResponse,
)

from catalog.api import fga_deps, lineage_deps
from catalog.api.dependencies import FgaClientDep, LineageEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import versions as version_door
from service_kit.governed.oidc import IDToken
from service_kit.governed.person_identity import PersonPrincipal
from service_kit.governed.principal import Principal
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")


def _app(ns: object, monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, list[list[str]]]:
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(version_door.router)
    settings = SimpleNamespace(storage_options=dict, warehouses_enabled=False, fga_enabled=False, root="")
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: ns
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: PersonPrincipal(
        principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    )
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
    assert "CreateTableVersion" in answer.json()["detail"]


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
