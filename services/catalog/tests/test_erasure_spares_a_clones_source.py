"""An erasure neither rewrites nor reclaims a table another dataset resolves its files through (#114).

A shallow clone's manifest names the source in ``base_paths`` and its data files resolve through the
source's (``lance_docs/file_format.md`` "Shallow Clone"), so the source's files are the clone's only
copy. The compact and GC doors refuse to touch such a source — ``maintenance.require_compactable`` and
``require_reclaimable`` run ``refuse_a_referring_datasets_source`` — because compacting then reclaiming
it breaks the clone. The erasure performs the same two verbs and must refuse the same way: the clone
still holds the subject, which is its own surface ([[LH-263]]), and a broken clone erases nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import lance
import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import LanceNamespace

from catalog.api.dependencies import get_namespace, get_storage_options
from catalog.api.v1.endpoints import erasure as door
from catalog.core.config import Settings, get_settings
from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table
from catalog.services.erasure import ErasureReport, erase
from service_kit.lakehouse.base_refs import BaseRefs, normalise
from service_kit.lakehouse.ns_errors import install_problem_handlers


_SUBJECT = "alice"
_PREDICATE = "pii = 'alice'"


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def _holding(uri: str) -> list[str]:
    return [p.name for p in (Path(uri) / "data").rglob("*.lance") if _SUBJECT.encode() in p.read_bytes()]


def _erase_protected(tmp_path: Path) -> tuple[str, ErasureReport]:
    """main v1 holds the subject, v2 adds carol; ``work`` is cut from v2; the table's root is protected."""
    uri = str(tmp_path / "source")
    lance.write_dataset(_rows(_SUBJECT, "bob"), uri)
    lance.write_dataset(_rows("carol"), uri, mode="append")
    lance.dataset(uri).create_branch("work", 2)
    report = erase(
        lance.dataset(uri),
        reopen=lambda: lance.dataset(uri),
        storage_options={},
        protected=BaseRefs(protected={normalise(uri)}),
        table="t",
        predicate=_PREDICATE,
        retention=timedelta(0),
    )
    return uri, report


def test_a_protected_table_is_neither_rewritten_nor_reclaimed(tmp_path: Path) -> None:
    uri, report = _erase_protected(tmp_path)

    refused = {s.surface: s.outcome for s in report.surfaces if s.surface.startswith(("compact:", "history:"))}
    assert refused == {f"{step}:{ref}": "failed" for step in ("compact", "history") for ref in ("main", "work")}
    assert all("another dataset resolves its files through" in s.detail for s in report.surfaces if s.surface in refused)
    assert _holding(uri), "the source's data files are the clone's only copy, so none may be rewritten away"
    assert report.complete is False


def test_the_report_names_no_ref_whose_deletion_could_not_finish_it(tmp_path: Path) -> None:
    """``work``'s head stands on main v2's files, which a reclaim that RAN would keep for it. None ran,
    so deleting ``work`` would destroy a working ref and leave every residual where it is."""
    _, report = _erase_protected(tmp_path)

    verify = next(s.detail for s in report.surfaces if s.surface == "verify")
    assert report.residual_versions, "the refused reclaim must leave a residual for this to be the right test"
    assert (report.pinned_by, report.held_by_retention) == ([], [])
    assert f"{report.residual_versions} remain because the reclaim of ['main', 'work'] did not run" in verify


@pytest.fixture
def namespace(tmp_path: Path) -> LanceNamespace:
    """A table and, beside it, a shallow clone that resolves its files through the table's."""
    ns = lance_namespace.connect("dir", {"root": str(tmp_path / "data")})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, _rows(_SUBJECT, "bob").schema) as writer:
        writer.write_table(_rows(_SUBJECT, "bob"))
    create_table(ns, {}, ["subjects"], sink.getvalue().to_pybytes(), mode="create")
    open_dataset(ns, {}, ["subjects"]).insert(_rows("carol"))
    open_dataset(ns, {}, ["subjects"]).shallow_clone(str(tmp_path / "data" / "clone.lance"), (None, None))
    return ns


@pytest.fixture
def client(namespace: LanceNamespace) -> Iterator[TestClient]:
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s")
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: namespace
    application.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(application) as test_client:
        yield test_client


def test_the_door_leaves_a_clones_source_readable_through_the_clone(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/management/v1/table/subjects/erasure", json={"predicate": _PREDICATE})

    assert response.status_code == 200, response.text
    report = response.json()
    assert {s["surface"]: s["outcome"] for s in report["surfaces"] if s["surface"] in ("compact:main", "history:main")} == {
        "compact:main": "failed",
        "history:main": "failed",
    }
    assert sorted(lance.dataset(str(tmp_path / "data" / "clone.lance")).to_table().column("pii").to_pylist()) == ["alice", "bob", "carol"]
    assert report["complete"] is False
