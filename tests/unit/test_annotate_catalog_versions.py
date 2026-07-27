"""The annotator's version history + time-travel in CATALOG mode (task #99).

In full catalog mode (``MEDIA_READ/WRITE_BACKEND=catalog`` + ``MEDIA_CATALOG_URI``)
the catalog owns the version number-space: the ``/versions`` listing must come from
the catalog's ``version/list`` + version-pinned counts (it used to silently return
``[]`` off the absent local replica), and a ``?version=N`` wire read must be the
catalog's time-travel ``/query`` (it used to 404-or-empty off the local table). The
``X-Annotations-Version-Source`` header names the number-space in BOTH modes. The
REST transport is mocked at the :class:`CatalogTransport` seam — everything from the
route down through ``open_catalog_reader``/``CatalogTableReader`` is real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import lance
import pyarrow as pa
import pytest
from annotator.annotations.router import router as annotations_router
from common.core.config import Settings
from common.core.exceptions import NotFoundError
from common.core.handlers import register_handlers
from common.lancekit.descriptor import Declared
from common.lancekit.reader import CatalogVersion
from common.state import AppState
from fastapi import FastAPI
from fastapi.testclient import TestClient


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import QueryTableRequest

DOC = "a" * 16
_DECLARED = Declared.model_validate({"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}})
_UNIT_FILTER = f"doc_id = '{DOC}' AND speech_id = 0 AND chunk_id = 19"
_V2_MILLIS = 1_753_300_000_000
_V1_MILLIS = 1_753_200_000_000


class _FakeTransport:
    """A :class:`CatalogTransport` double: two catalog versions (v1 = 1 row, v2 = 2),
    recording every call so tests can assert WHAT reached the catalog."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.rows_by_version: dict[int, list[dict]] = {1: [{"id": "a1"}], 2: [{"id": "a1"}, {"id": "a2"}]}
        self.table_missing = False

    def _rows(self, version: int | None) -> list[dict]:
        if self.table_missing:
            raise NotFoundError("catalog table not found")
        if version is not None and version not in self.rows_by_version:
            raise NotFoundError(f"table version {version} not found")
        return self.rows_by_version[version or max(self.rows_by_version)]

    def query(self, request: QueryTableRequest) -> bytes:
        self.calls.append(("query", request))
        table = pa.Table.from_pylist(self._rows(request.version), schema=pa.schema([("id", pa.string())]))
        sink = pa.BufferOutputStream()
        with pa.ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)
        return sink.getvalue().to_pybytes()

    def count(self, table_id: list[str], filter: str | None, *, version: int | None = None) -> int:
        self.calls.append(("count", tuple(table_id), filter, version))
        return len(self._rows(version))

    def table_version(self, table_id: list[str]) -> int:
        self.calls.append(("table_version", tuple(table_id)))
        if self.table_missing:
            raise NotFoundError("catalog table not found")
        return max(self.rows_by_version)

    def list_versions(self, table_id: list[str], *, limit: int | None = None) -> list[CatalogVersion]:
        self.calls.append(("list_versions", tuple(table_id), limit))
        if self.table_missing:
            raise NotFoundError("catalog table not found")
        listed = [
            CatalogVersion(version=2, timestamp_millis=_V2_MILLIS),
            CatalogVersion(version=1, timestamp_millis=_V1_MILLIS),
        ]
        return listed[:limit] if limit is not None else listed


class _Registry:
    def __init__(self, handle: object) -> None:
        self._handle = handle

    def get(self, dataset_id: str) -> object:
        return self._handle

    def default(self) -> object:
        return self._handle


def _make_client(settings: Settings, handle: object) -> TestClient:
    app = FastAPI()
    register_handlers(app)
    app.include_router(annotations_router)
    app.state.resources = AppState(settings=settings, registry=_Registry(handle))
    return TestClient(app)


@pytest.fixture
def catalog_harness(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _FakeTransport]:
    """The annotations routes in FULL catalog mode, REST transport mocked at the seam."""
    transport = _FakeTransport()
    monkeypatch.setattr("common.lancekit.reader.RestCatalogTransport", lambda *a, **k: transport)
    settings = Settings(
        MEDIA_READ_BACKEND="catalog",
        MEDIA_WRITE_BACKEND="catalog",
        MEDIA_CATALOG_URI="http://catalog.test",
        MEDIA_CATALOG_NAMESPACE="proj1",
    )
    handle = SimpleNamespace(id="ds1", descriptor=SimpleNamespace(declared=_DECLARED))
    return _make_client(settings, handle), transport


def test_catalog_mode_versions_listing_counts_and_header(catalog_harness) -> None:
    client, transport = catalog_harness
    response = client.get(f"/api/annotations/{DOC}/0/19/versions")
    assert response.status_code == 200, response.text
    assert response.headers["X-Annotations-Version-Source"] == "catalog"  # the catalog number-space
    assert response.json() == [
        {
            "version": 2,
            "timestamp": datetime.fromtimestamp(_V2_MILLIS / 1000, tz=UTC).isoformat(),
            "count": 2,
        },
        {
            "version": 1,
            "timestamp": datetime.fromtimestamp(_V1_MILLIS / 1000, tz=UTC).isoformat(),
            "count": 1,
        },
    ]
    # ONE governed listing (settings-derived id, route limit) + one count PER version,
    # pinned at that version and filtered to THIS unit.
    assert ("list_versions", ("proj1", "annotations"), 20) in transport.calls
    counts = [call for call in transport.calls if call[0] == "count"]
    assert [(call[3], call[2]) for call in counts] == [(2, _UNIT_FILTER), (1, _UNIT_FILTER)]


def test_catalog_mode_versions_listing_respects_limit(catalog_harness) -> None:
    client, transport = catalog_harness
    response = client.get(f"/api/annotations/{DOC}/0/19/versions?limit=1")
    assert response.status_code == 200
    assert [entry["version"] for entry in response.json()] == [2]  # most-recent first, capped
    assert ("list_versions", ("proj1", "annotations"), 1) in transport.calls


def test_catalog_mode_missing_table_degrades_to_empty_listing(catalog_harness) -> None:
    # No annotations table in the catalog yet = no history (not an error) — the same
    # degrade as a missing local table, but now EXPLICITLY catalog-sourced.
    client, transport = catalog_harness
    transport.table_missing = True
    response = client.get(f"/api/annotations/{DOC}/0/19/versions")
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Annotations-Version-Source"] == "catalog"


def test_catalog_mode_reclaimed_version_drops_entry_not_listing(catalog_harness) -> None:
    # v1 reclaimed (maintenance retention) between the list and its count: that entry
    # drops; the rest of the history still serves.
    client, transport = catalog_harness
    del transport.rows_by_version[1]
    response = client.get(f"/api/annotations/{DOC}/0/19/versions")
    assert response.status_code == 200
    assert [entry["version"] for entry in response.json()] == [2]


def test_catalog_mode_time_travel_read_rows_and_headers(catalog_harness) -> None:
    client, transport = catalog_harness
    response = client.get(f"/api/annotations/{DOC}/0/19?version=1")
    assert response.status_code == 200, response.text
    table = pa.ipc.open_stream(response.content).read_all()
    assert table.column("id").to_pylist() == ["a1"]  # v1's single row, not the latest two
    assert response.headers["X-Annotations-Version"] == "1"
    assert response.headers["X-Annotations-Version-Source"] == "catalog"  # honest number-space
    request = next(call[1] for call in transport.calls if call[0] == "query")
    assert request.version == 1  # the pin rode the catalog /query
    assert request.filter == _UNIT_FILTER


def test_catalog_mode_bad_version_is_a_404_problem(catalog_harness) -> None:
    client, _ = catalog_harness
    response = client.get(f"/api/annotations/{DOC}/0/19?version=999")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_catalog_mode_current_read_still_catalog_sourced(catalog_harness) -> None:
    # Regression guard for the restructured branch: the no-version read keeps its
    # catalog-sourced version header + rows.
    client, _ = catalog_harness
    response = client.get(f"/api/annotations/{DOC}/0/19")
    assert response.status_code == 200, response.text
    assert pa.ipc.open_stream(response.content).read_all().num_rows == 2
    assert response.headers["X-Annotations-Version"] == "2"
    assert response.headers["X-Annotations-Version-Source"] == "catalog"


class _LocalHandle:
    """The slice of ``DatasetHandle`` the local routes touch, over a tmp dataset root."""

    def __init__(self, root: Path) -> None:
        self.id = "ds1"
        self.path = root
        self.storage_options = None
        self.descriptor = SimpleNamespace(declared=_DECLARED)

    def table_uri(self, table: str) -> str:
        return str(self.path / f"{table}.lance")

    def sync_table_info(self, table: str, observed_version: int) -> None:
        pass  # descriptor drift-sync is the registry's concern, not this test's


def _seed_local(root: Path) -> None:
    """Two local versions: v1 = 1 row for the unit, v2 adds one more (+ another unit's)."""
    schema = pa.schema([("doc_id", pa.string()), ("speech_id", pa.int64()), ("chunk_id", pa.int64()), ("id", pa.string())])
    unit = {"doc_id": DOC, "speech_id": 0, "chunk_id": 19}
    other = {"doc_id": DOC, "speech_id": 0, "chunk_id": 99}
    uri = str(root / "annotations.lance")
    lance.write_dataset(pa.Table.from_pylist([{**unit, "id": "a1"}], schema=schema), uri)
    lance.dataset(uri).merge_insert("id").when_not_matched_insert_all().execute(
        pa.Table.from_pylist([{**unit, "id": "a2"}, {**other, "id": "b1"}], schema=schema)
    )


def test_local_mode_listing_unchanged_and_header_says_local(tmp_path: Path) -> None:
    # Local (non-catalog) mode keeps the ds.versions()/checkout listing — and now names
    # its number-space explicitly.
    _seed_local(tmp_path)
    client = _make_client(Settings(), _LocalHandle(tmp_path))
    response = client.get(f"/api/annotations/{DOC}/0/19/versions")
    assert response.status_code == 200, response.text
    assert response.headers["X-Annotations-Version-Source"] == "local"
    assert [(entry["version"], entry["count"]) for entry in response.json()] == [(2, 2), (1, 1)]


def test_local_mode_time_travel_read_unchanged(tmp_path: Path) -> None:
    _seed_local(tmp_path)
    client = _make_client(Settings(), _LocalHandle(tmp_path))
    response = client.get(f"/api/annotations/{DOC}/0/19?version=1")
    assert response.status_code == 200, response.text
    table = pa.ipc.open_stream(response.content).read_all()
    assert table.column("id").to_pylist() == ["a1"]
    assert response.headers["X-Annotations-Version-Source"] == "local"


def test_mixed_config_keeps_the_local_path() -> None:
    # catalog backends WITHOUT a URI (the in-process fallback) is NOT full catalog mode:
    # the version surface must stay local (pre-merge safety — same gate as wire/save).
    settings = Settings(MEDIA_READ_BACKEND="catalog", MEDIA_WRITE_BACKEND="catalog")
    assert not settings.rest_catalog_mode
    client = _make_client(settings, _LocalHandle(Path("/nonexistent")))
    response = client.get(f"/api/annotations/{DOC}/0/19/versions")
    assert response.status_code == 200
    assert response.json() == []  # missing local table degrades exactly as before
    assert response.headers["X-Annotations-Version-Source"] == "local"
