"""An erasure never drops a registered clone's tag on its source, and stays open while the tag holds the subject.

A shallow clone reads its source's data files where they are (``lance_docs/lance/format/table/layout.md`` "Shallow
Clone"), and the catalog keeps the version a registered clone reads by a tag it takes on the source (ADR 0198 §1,
:mod:`lakehouse.clone_sources`). Dropping that tag would let a cleanup take files the clone has no other copy of, so the
verification keeps it, names it ``held``, and the obligation stays open until the clone ends: the clone holds the
subject too, which is its own surface ([[LH-263]]). The job here rewrites the source (the conftest's ``ErasureJob``); the
tag keeps the version the clone reads while the source's own history is reclaimed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import lance
import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, LanceNamespace
from tables_by_name import open_by_name

from catalog.api.dependencies import get_namespace, get_storage_options
from catalog.api.v1.endpoints import erasure as door
from catalog.core.config import Settings, get_settings
from catalog.core.identifiers import with_minted_uuid
from catalog.services.dataplane import create_table, read_arrow_body
from lakehouse import base_registry, clone_sources
from lakehouse.ns_errors import install_problem_handlers
from lakehouse.storage_options import StorageOptions


#: The catalog's default body cap (LANCE_MAX_BODY_BYTES), far above every body here.
_BODY_LIMIT = 256 * 1024 * 1024


_SUBJECT = "alice"
_PREDICATE = "pii = 'alice'"


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


@pytest.fixture
def namespace(tmp_path: Path) -> LanceNamespace:
    """A table and, beside it, a shallow clone that resolves its files through the table's."""
    ns = lance_namespace.connect("dir", {"root": str(tmp_path / "data")})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, _rows(_SUBJECT, "bob").schema) as writer:
        writer.write_table(_rows(_SUBJECT, "bob"))
    ns.create_namespace(CreateNamespaceRequest(id=["db"], properties=with_minted_uuid(None)))
    create_table(
        ns,
        StorageOptions(),
        ["db", "subjects"],
        read_arrow_body(sink.getvalue().to_pybytes(), max_bytes=_BODY_LIMIT),
        root=str(tmp_path / "data"),
        mode="create",
        registry=base_registry.BaseRegistry(control_root=str(tmp_path / "control")),
        holder="db$subjects",
    )
    open_by_name(ns, StorageOptions(), ["db", "subjects"]).insert(_rows("carol"))
    source = open_by_name(ns, StorageOptions(), ["db", "subjects"])
    clone = str(tmp_path / "data" / "clone.lance")
    source.shallow_clone(clone, (None, None))
    # The tag the register door takes on the source for the clone, at the version the clone reads.
    source.tags.create(clone_sources.tag_of(clone), source.version)
    return ns


@pytest.fixture
def client(namespace: LanceNamespace, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(s3_access_key_id="k", s3_secret_access_key="s", LANCE_CONTROL_ROOT=str(tmp_path / "control"))
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: namespace
    application.dependency_overrides[get_storage_options] = lambda: StorageOptions()
    with TestClient(application) as test_client:
        yield test_client


def test_the_door_leaves_a_clones_source_readable_through_the_clone(client: TestClient, namespace: LanceNamespace, tmp_path: Path, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    recorded = client.post("/management/v1/table/db$subjects/erasure", json={"predicate": _PREDICATE})
    assert recorded.status_code == 202, recorded.text
    erasure_job(open_by_name(namespace, StorageOptions(), ["db", "subjects"]).uri, _PREDICATE).run()

    response = client.post(f"/management/v1/table/db$subjects/erasure/{recorded.json()['obligation_id']}/verify", json={"predicate": _PREDICATE})

    assert response.status_code == 200, response.text
    verification = response.json()
    clone = str(tmp_path / "data" / "clone.lance")
    outcomes = {s["surface"]: s["outcome"] for s in verification["report"]["surfaces"]}
    assert outcomes[f"tag:{clone_sources.tag_of(clone)}"] == "held"
    assert (outcomes["compact:main"], outcomes["history:main"]) == ("clean", "reclaimed")
    assert sorted(lance.dataset(clone).to_table().column("pii").to_pylist()) == ["alice", "bob", "carol"]
    assert verification["obligation"]["state"] == "open"
