"""An erasure's verification asks the GC door's manifest-flag gate of every ref before reclaiming it.

The GC door refuses a dataset whose manifest carries a flag its reclaim has not been checked against
(``maintenance.require_reclaimable``, which asks the sweep's ``describe_gc_unsupported_flags``). The
verification reclaims every clean ref through that ref's own handle, so it refuses the same way and says so
per ref. The job's compaction is ``version/create``'s to judge (``maintenance.require_compactable``, ADR
0162), so nothing here compacts.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import lance
import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace, get_storage_options
from catalog.api.v1.endpoints import erasure as door
from catalog.core.config import Settings, get_settings
from catalog.services.dataplane import create_table, read_arrow_body
from catalog.services.erasure import ErasureReport, ref_heads, verify
from service_kit.lakehouse import base_refs
from service_kit.lakehouse.base_refs import BaseRefs


#: The catalog's default body cap (LANCE_MAX_BODY_BYTES), far above every body here.
_BODY_LIMIT = 256 * 1024 * 1024


_PREDICATE = "pii = 'alice'"


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def test_a_table_mixing_file_versions_is_not_reclaimed_on_any_ref(tmp_path: Path, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    """pylance 12 stamps reader flag 256 on an append at another file version, and no operation removes it, so
    every maintenance gate refuses the table: no reclaim here has been checked against that layout."""
    uri = str(tmp_path / "mixed")
    lance.write_dataset(_rows("alice", "bob", "carol", "dan"), uri, data_storage_version="2.1", enable_stable_row_ids=True, max_rows_per_file=2)
    lance.write_dataset(_rows("eve"), uri, mode="append", data_storage_version="2.2")
    lance.dataset(uri).create_branch("work")
    recorded = ref_heads(lance.dataset(uri))
    erasure_job(uri, _PREDICATE).run()

    report = verify(
        lance.dataset(uri),
        storage_options={},
        protected=None,
        reopen=lambda: lance.dataset(uri),
        table="t",
        predicate=_PREDICATE,
        retention=timedelta(0),
        trusted=recorded,
    )

    history = {s.surface: (s.outcome, s.detail) for s in report.surfaces if s.surface.startswith("history:")}
    assert {surface: outcome for surface, (outcome, _) in history.items()} == {"history:main": "failed", "history:work": "failed"}
    assert all("256 (mixed data file versions)" in detail for _, detail in history.values()), history
    assert (report.complete, report.settled) == (False, False)


def test_the_door_reads_the_store_the_table_lives_in_with_the_requests_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hop the verification door takes to the store carries the request's storage options: the #114
    pre-pass's listing, the verification and its reopen. Without them an `s3://` table is read as a local path,
    and the listing and the reopen fail."""
    ns = lance_namespace.connect("dir", {"root": str(tmp_path / "data")})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, _rows("alice").schema) as writer:
        writer.write_table(_rows("alice"))
    ns.create_namespace(lance_namespace.CreateNamespaceRequest(id=["db"]))
    create_table(
        ns,
        {},
        ["db", "subjects"],
        read_arrow_body(sink.getvalue().to_pybytes(), max_bytes=_BODY_LIMIT),
        root=str(tmp_path / "data"),
        mode="create",
        registry=None,
    )
    so = {"aws_region": "eu-north-1"}
    seen: dict[str, Any] = {}
    listed: list[dict[str, str]] = []
    siblings = base_refs.sibling_base_refs

    def _record(dataset: Any, **kwargs: Any) -> ErasureReport:
        seen.update(kwargs)
        return ErasureReport(table="db$subjects", predicate=_PREDICATE)

    def _listing(location: str, storage_options: dict[str, str], *, configured: list[str], record_of: Any) -> BaseRefs:
        listed.append(storage_options)
        return siblings(location, storage_options, configured=configured, record_of=record_of)

    monkeypatch.setattr(door.erasure, "verify", _record)
    monkeypatch.setattr(base_refs, "sibling_base_refs", _listing)
    application = FastAPI()
    application.include_router(door.router)
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_CONTROL_ROOT=str(tmp_path / "control"))
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: ns
    application.dependency_overrides[get_storage_options] = lambda: so
    with TestClient(application) as client:
        recorded = client.post("/management/v1/table/db$subjects/erasure", json={"predicate": _PREDICATE})
        assert recorded.status_code == 202, recorded.text
        response = client.post(f"/management/v1/table/db$subjects/erasure/{recorded.json()['obligation_id']}/verify", json={"predicate": _PREDICATE})

    assert response.status_code == 200, response.text
    assert seen["storage_options"] == so
    assert listed == [so]
    assert _options_the_reopen_passes(seen["reopen"], monkeypatch) == [so]


def _options_the_reopen_passes(reopen: Callable[[], Any], monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Call ``reopen`` and answer the ``storage_options`` each `lance.dataset` it made was handed."""
    opened: list[Any] = []
    real = lance.dataset
    signature = inspect.signature(real)

    def _dataset(*args: Any, **kwargs: Any) -> lance.LanceDataset:
        opened.append(signature.bind(*args, **kwargs).arguments.get("storage_options"))
        return real(*args, **kwargs)

    monkeypatch.setattr(lance, "dataset", _dataset)
    reopen()
    return opened
