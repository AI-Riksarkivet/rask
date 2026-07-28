"""Unit tests for the IIIF→bronze ingest head (P7a, re-tiered by R23) — the producer that replaced
rask's prefetch lane. Bronze is the first governed tier; the IIIF Image API is external raw.

Infra-free: IIIF rides an ``httpx.MockTransport`` (no network), the Lance write lands on a local tmp
path (the established unit pattern for the compute seams), and a fake Dapr client is the capture
transport for the emitted lineage. Pinned here: the head's contract (disabled → explicit refusal,
publish failure → retryable), the harvested BRONZE page dataset's superset schema
(id/payload/source_uri/volume_id/page_key/stage — design §2.2 + the merged raw→bronze stage stamp), and
the drift pins tying the self-contained ``scripts/ray_iiif_ingest_job.py`` helpers to ``storage.iiif``
(the job cannot import ``services/``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import lance
import pytest
from dapr.aio.clients import DaprClient
from medallion.core.config import MedallionSettings
from medallion.schemas.htr import GOLD_CONTRACT_COLUMNS
from medallion.services.iiif_produce import IIIFVolumeSource, _harvest_and_ingest, ingest_iiif
from medallion.services.ingest import IngestResult

from storage import iiif as storage_iiif


def _load_harvest_job() -> ModuleType:
    """Load the standalone job by path (it is baked into the ray image, not an installed package)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "ray_iiif_ingest_job.py"
    spec = importlib.util.spec_from_file_location("ray_iiif_ingest_job", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harvest_job = _load_harvest_job()


_VOLUME = "A0068688"
_MANIFEST = {
    "items": [
        {"id": f"https://iiifintern-ai.ra.se/arkis!{_VOLUME}_00002/canvas"},
        {"id": f"https://iiifintern-ai.ra.se/arkis!{_VOLUME}_00001/canvas"},
    ]
}


class _FakeDapr:
    """Records publish_event calls; optionally fails to exercise the publish-failure path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_: Any) -> None:
        if self.fail:
            raise RuntimeError("sidecar down")
        self.published.append((topic_name, json.loads(data)))


def _settings(**overrides: Any) -> MedallionSettings:
    values: dict[str, Any] = {"compute_enabled": True, "iiif_bronze_uri": "memory://unused"}
    values.update(overrides)
    return MedallionSettings.model_validate(values)


_RESULT = IngestResult(
    version=2,
    row_count=2,
    source_uris=[f"https://iiifintern-ai.ra.se/arkis!{_VOLUME}_0000{n}/full/max/0/default.jpg" for n in (1, 2)],
    fields=[{"name": "payload", "type": "blob"}, {"name": "page_key", "type": "string"}],
)


def _mock_iiif_client() -> httpx.Client:
    """An httpx client over a MockTransport serving the manifest + two deterministic page images."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest"):
            return httpx.Response(200, json=_MANIFEST)
        page = request.url.path.split("arkis!", 1)[-1].split("/", 1)[0]
        return httpx.Response(200, content=f"JPEG-BYTES-{page}".encode())

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_disabled_head_refuses_instead_of_dummying() -> None:
    """No IIIF config → an explicit iiif_disabled (the route's 409): real pages can't be faked."""
    dapr = _FakeDapr()
    result = asyncio.run(ingest_iiif(cast(DaprClient, dapr), MedallionSettings.model_validate({}), _VOLUME))
    assert result == {"status": "iiif_disabled"}
    assert dapr.published == []


def test_publish_failure_surfaces_as_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    import medallion.services.iiif_produce as mod

    monkeypatch.setattr(mod, "_harvest_and_ingest", lambda *_a: _RESULT)
    result = asyncio.run(ingest_iiif(cast(DaprClient, _FakeDapr(fail=True)), _settings(), _VOLUME))
    assert result["status"] == "publish_failed"  # → the route's 503 + Retry-After


def test_project_without_routing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from service_kit.lakehouse.warehouse_registry import UnresolvableProjectError

    with pytest.raises(UnresolvableProjectError):
        asyncio.run(ingest_iiif(cast(DaprClient, _FakeDapr()), _settings(), _VOLUME, project="acme"))


def test_harvest_writes_the_bronze_page_dataset_with_provenance(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-process harvest lands the blob-v2 page dataset with the §2.2 superset schema (+ the
    ingest-time bronze stage stamp, R23), page order sorted by image id (reproducible positional ids)."""
    bronze_uri = str(tmp_path / "bronze_pages")
    settings = _settings(iiif_bronze_uri=bronze_uri)
    client = _mock_iiif_client()

    def _source_with_mock_client(volume_id: str, *, base_url: str, query_params: str, timeout: float, max_pages: int | None = None) -> IIIFVolumeSource:
        return IIIFVolumeSource(volume_id, base_url=base_url, query_params=query_params, timeout=timeout, max_pages=max_pages, client=client)

    monkeypatch.setattr("medallion.services.iiif_produce.IIIFVolumeSource", _source_with_mock_client)

    result = _harvest_and_ingest(settings, bronze_uri, _VOLUME, None)

    assert result.row_count == 2
    dataset = lance.dataset(bronze_uri)
    assert dataset.schema.names == ["id", "payload", "source_uri", "volume_id", "page_key", "stage"]
    assert set(dataset.to_table(columns=["stage"]).column("stage").to_pylist()) == {"bronze"}
    table = dataset.to_table(columns=["id", "source_uri", "volume_id", "page_key"])
    assert table.column("page_key").to_pylist() == [f"{_VOLUME}_00001", f"{_VOLUME}_00002"]  # sorted, not manifest order
    assert table.column("volume_id").to_pylist() == [_VOLUME] * 2
    assert all(uri.startswith("https://") and "arkis!" in uri for uri in table.column("source_uri").to_pylist())
    # blob-aware schema facet at the page head — the bronze dataset IS the page cache now.
    assert {"name": "payload", "type": "blob"} in result.fields


def test_max_pages_bounds_the_harvest(tmp_path: Any) -> None:
    bronze_uri = str(tmp_path / "bronze_pages")
    source = IIIFVolumeSource(
        _VOLUME, base_url="https://iiifintern-ai.ra.se", query_params="full/max/0/default.jpg", timeout=5.0, max_pages=1, client=_mock_iiif_client()
    )
    objects = list(source.iter_objects())
    assert len(objects) == 1
    assert objects[0].data == f"JPEG-BYTES-{_VOLUME}_00001".encode()
    assert bronze_uri  # tmp_path kept for symmetry; nothing written in this source-only test


def test_ray_job_helpers_are_drift_pinned_to_storage_iiif() -> None:
    """The self-contained Ray harvest job mirrors ``storage.iiif``'s manifest/URL shapes byte-for-byte
    (it cannot import ``services/`` or ``packages/`` — the repo's self-contained-ray-job convention)."""
    assert harvest_job.DEFAULT_IIIF_BASE == storage_iiif.DEFAULT_IIIF_BASE
    assert harvest_job.DEFAULT_QUERY_PARAMS == storage_iiif.DEFAULT_QUERY_PARAMS
    assert harvest_job.image_url("A0068688_00001") == storage_iiif.build_image_url("A0068688_00001")
    assert harvest_job.parse_image_ids(_MANIFEST) == sorted(f"{_VOLUME}_0000{n}" for n in (1, 2))
    # get_image_ids over the same manifest via the mock transport must agree with the job's parser.
    assert storage_iiif.get_image_ids(_VOLUME, client=_mock_iiif_client()) == harvest_job.parse_image_ids(_MANIFEST)


def test_gold_contract_is_pinned() -> None:
    """Design §2.4: the gold transcription schema is load-bearing — a dropped field is unrecoverable
    downstream (the exporter's ALTO 4.4 serializer needs every one). Changing this tuple is a
    deliberate contract change reviewed with the P7b movers + the exporter together."""
    assert GOLD_CONTRACT_COLUMNS == (
        "volume_id",
        "page_key",
        "page_width",
        "page_height",
        "region_polygons",
        "line_polygons",
        "reading_order",
        "text",
        "confidences",
        "source_rowid",
    )
