"""#1 — the LIVE cascade emits the standard ``columnLineage`` facet (producer side of #24).

The edge STORE (parser + Cypher + read endpoints) already exists; the gap was that only ``seed.py``
emitted the facet, so real runs produced zero column edges. These tests pin the producer half and prove
it round-trips through the ACTUAL consumer parser (``lineage.models.Dataset.column_edges``):

* the shared ``column_lineage_facet`` helper groups flat edges into the spec shape;
* the generic compute DECLARES the right edges (IDENTITY for carried columns, TRANSFORMATION for derived
  blob artifacts, none for the ``stage`` stamp);
* ``build_run_event`` attaches the facet on the OUTPUT pointing at the single upstream input;
* ``measure_stage`` RECONSTRUCTS those edges from the on-disk schemas alone, for a write this process never
  saw (the Ray job) — without it the facet is empty on exactly the seam production runs;
* a real ``handle_stage`` transform emits edges the consumer parses back out — in-process AND via Ray.
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any, cast

import lance
import medallion.services.transform as mover
import pyarrow as pa
import pytest
from dapr.aio.clients import DaprClient
from lineage.models import Dataset
from medallion.core.config import MedallionSettings
from medallion.schemas.events import build_run_event
from medallion.services.compute import _column_map, measure_stage, seed_raw, transform_stage
from medallion.services.transform import handle_stage

from service_kit.openlineage import column_lineage_facet


# --------------------------------------------------------------------------- #
# the shared facet helper + consumer round-trip
# --------------------------------------------------------------------------- #


def test_column_lineage_facet_groups_by_output_and_roundtrips() -> None:
    # Two inputs to the SAME output column must group under one out_field with two inputFields.
    edges = [
        ("hash", "bronze", "events", "name", "DIRECT", "TRANSFORMATION", True),
        ("hash", "bronze", "events", "salt", "DIRECT", "TRANSFORMATION", True),
        ("id", "bronze", "events", "id", "DIRECT", "IDENTITY", False),
    ]
    facet = column_lineage_facet("producer", edges)
    assert cast("str", facet["_schemaURL"]).endswith("ColumnLineageDatasetFacet")
    fields = cast("dict[str, Any]", facet["fields"])
    assert set(fields) == {"hash", "id"}
    assert len(fields["hash"]["inputFields"]) == 2

    # The ACTUAL consumer parser recovers the flattened edges — masking preserved, source (name, field).
    parsed = Dataset(namespace="bronze", name="events", facets={"columnLineage": facet}).column_edges
    hash_edges = {(e["field"], e["masking"]) for e in parsed if e["out_field"] == "hash"}
    assert hash_edges == {("name", True), ("salt", True)}
    id_edge = next(e for e in parsed if e["out_field"] == "id")
    assert (id_edge["name"], id_edge["field"], id_edge["subtype"]) == ("events", "id", "IDENTITY")


def test_column_lineage_facet_drops_malformed_and_empty() -> None:
    # An empty output/source column must not materialise a junk vertex; no valid edges → no facet at all.
    assert column_lineage_facet("p", [("", "ns", "d", "f", "DIRECT", "", False)]) == {}
    assert column_lineage_facet("p", [("out", "ns", "d", "", "DIRECT", "", False)]) == {}
    assert column_lineage_facet("p", []) == {}


# --------------------------------------------------------------------------- #
# the compute DECLARES the right edges (pure _column_map)
# --------------------------------------------------------------------------- #


def test_column_map_identity_for_carried_columns_only() -> None:
    in_schema = pa.schema([pa.field("id", pa.int64()), pa.field("payload", pa.string())])
    deps = _column_map(in_schema, ["id", "payload", "stage"], blob_cols=set())
    # Every carried column is IDENTITY; the ``stage`` provenance stamp is a constant with NO input.
    assert set(deps) == {("id", "id", "IDENTITY"), ("payload", "payload", "IDENTITY")}
    assert not any(out == "stage" for out, _in, _t in deps)


def test_column_map_transformation_for_derived_artifacts() -> None:
    in_schema = pa.schema([pa.field("id", pa.int64()), pa.field("payload", pa.large_binary())])
    deps = _column_map(in_schema, ["id", "payload", "stage", "thumbnail", "embedding"], blob_cols={"payload"})
    assert ("thumbnail", "payload", "TRANSFORMATION") in deps
    assert ("embedding", "payload", "TRANSFORMATION") in deps
    assert ("payload", "payload", "IDENTITY") in deps  # the blob itself is carried forward


# --------------------------------------------------------------------------- #
# the DISTRIBUTED reconstruction — measure_stage recovers the edges from disk
# --------------------------------------------------------------------------- #


def test_measure_stage_reconstructs_identity_edges_from_disk(tmp_path: Any) -> None:
    # The Ray job writes out-of-process, so nothing in this process saw the transform: measure_stage must
    # recover the SAME edges transform_stage declared, from the upstream + written schemas alone.
    raw, bronze = str(tmp_path / "raw"), str(tmp_path / "bronze")
    seed_raw(raw, {}, rows=3)  # columns [id, payload]
    declared = transform_stage(raw, bronze, {}, stage="bronze").column_map

    result = measure_stage(raw, bronze, {})

    assert set(result.column_map) == set(declared)
    # source_rowid is minted at the head from the raw row's reserved _rowid metacolumn (root provenance);
    # id/payload are carried IDENTITY. measure_stage recovers the SAME head edge from the two schemas alone.
    assert set(result.column_map) == {
        ("id", "id", "IDENTITY"),
        ("payload", "payload", "IDENTITY"),
        ("source_rowid", "_rowid", "IDENTITY"),
    }
    assert result.row_count == 3 and result.version > 0  # still a real measurement, not just edges


def test_measure_stage_reconstructs_transformation_edges_for_a_media_stage(tmp_path: Any) -> None:
    # The media lane: thumbnail/embedding exist downstream but NOT upstream → TRANSFORMATION from the blob
    # column the deriver dispatched on, recovered without re-reading a single payload.
    bronze, silver = str(tmp_path / "bronze_media"), str(tmp_path / "silver_media")
    _write_media_dataset(bronze, [_png((200, 40, 40)), _png((40, 40, 200))])
    transform_stage(bronze, silver, {}, stage="silver")

    deps = set(measure_stage(bronze, silver, {}).column_map)

    assert ("thumbnail", "payload", "TRANSFORMATION") in deps
    assert ("embedding", "payload", "TRANSFORMATION") in deps
    assert ("payload", "payload", "IDENTITY") in deps
    assert ("id", "id", "IDENTITY") in deps
    assert not any(out == "stage" for out, _in, _t in deps)


# --------------------------------------------------------------------------- #
# build_run_event attaches the facet on the output, pointing at the single input
# --------------------------------------------------------------------------- #


def test_build_run_event_attaches_columnlineage_on_output() -> None:
    event = build_run_event(
        operation="ingest_events",
        author="data_eng",
        job_namespace="medallion",
        inputs=[("raw", "raw_events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=2,
        column_map=[("id", "id", "IDENTITY"), ("hash", "payload", "TRANSFORMATION")],
        token="t1",
    )
    output = Dataset.model_validate(event["outputs"][0])
    edges = {(e["out_field"], e["name"], e["field"], e["subtype"]) for e in output.column_edges}
    assert edges == {
        ("id", "raw_events", "id", "IDENTITY"),
        ("hash", "raw_events", "payload", "TRANSFORMATION"),
    }
    # The facet lives ONLY on the output — inputs never carry columnLineage.
    assert "columnLineage" not in (event["inputs"][0].get("facets") or {})


def test_build_run_event_without_column_map_omits_facet() -> None:
    event = build_run_event(
        operation="ingest_events",
        author=None,
        job_namespace="medallion",
        inputs=[("raw", "raw_events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=1,
        token="t1",
    )
    assert "columnLineage" not in (event["outputs"][0].get("facets") or {})


def test_build_run_event_failed_run_carries_no_column_lineage() -> None:
    # A FAIL keeps a bare output — no version, no schema, and no columnLineage (it derived nothing).
    event = build_run_event(
        operation="ingest_events",
        author=None,
        job_namespace="medallion",
        inputs=[("raw", "raw_events")],
        output_namespace="bronze",
        output_name="bronze$events",
        column_map=[("id", "id", "IDENTITY")],
        token="t1",
        event_type="FAIL",
        error_message="boom",
    )
    assert "columnLineage" not in (event["outputs"][0].get("facets") or {})


# --------------------------------------------------------------------------- #
# the WIRING — a real transform emits edges the consumer parses back out
# --------------------------------------------------------------------------- #


class _FakeDapr:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})


def _mover_settings(raw: str, bronze: str) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "from_uri": raw,
            "to_uri": bronze,
            "from_namespace": "raw",
            "from_dataset": "raw_events",
            "to_namespace": "bronze",
            "to_dataset": "bronze$events",
            "operation": "ingest_events",
            "pub_topic": "bronze.ready",
        }
    )


def test_handle_stage_emits_identity_column_edges(tmp_path: Any) -> None:
    raw, bronze = str(tmp_path / "raw"), str(tmp_path / "bronze")
    seed_raw(raw, {}, rows=4)  # columns [id, payload]
    dapr = _FakeDapr()
    settings = _mover_settings(raw, bronze)

    result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t1"}}))
    assert result == {"status": "SUCCESS"}

    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic)
    output = Dataset.model_validate(lineage["data"]["outputs"][0])
    edges = {(e["out_field"], e["name"], e["field"], e["subtype"]) for e in output.column_edges}
    # The LIVE cascade (not seed.py) produced field-to-field identity edges pointing at the raw input —
    # including source_rowid, minted at the head from the raw row's reserved _rowid metacolumn (root
    # provenance: the bronze row names the exact raw row it descends from).
    assert edges == {
        ("id", "raw_events", "id", "IDENTITY"),
        ("payload", "raw_events", "payload", "IDENTITY"),
        ("source_rowid", "raw_events", "_rowid", "IDENTITY"),
    }


def _png(color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _write_media_dataset(uri: str, payloads: list[bytes]) -> None:
    """A blob-v2 (image) upstream — the media lane's ``[id, payload:blob]`` shape."""
    from lance import blob_field

    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload")])
    table = pa.table({"id": list(range(len(payloads))), "payload": lance.blob_array(payloads)}, schema=schema)
    lance.write_dataset(table, uri, data_storage_version="2.2", enable_stable_row_ids=True)


def test_transform_stage_derives_artifact_column_edges(tmp_path: Any) -> None:
    # A blob (image) stage: the derived thumbnail/embedding edges are TRANSFORMATION from the blob column,
    # the blob + id are carried IDENTITY — proving the media lane populates the field-to-field graph.
    uri, silver = str(tmp_path / "bronze_media"), str(tmp_path / "silver_media")
    _write_media_dataset(uri, [_png((200, 40, 40)), _png((40, 40, 200))])

    result = transform_stage(uri, silver, {}, stage="silver")
    deps = set(result.column_map)
    assert ("thumbnail", "payload", "TRANSFORMATION") in deps
    assert ("embedding", "payload", "TRANSFORMATION") in deps
    assert ("id", "id", "IDENTITY") in deps
    assert ("payload", "payload", "IDENTITY") in deps


# --------------------------------------------------------------------------- #
# the RAY branch — the production seam emits the SAME edges, reconstructed
# --------------------------------------------------------------------------- #


def _ray_job_write(from_uri: str, to_uri: str, stage: str) -> None:
    """Stand in for ``scripts/ray_stage_job.py``: write the downstream dataset the way the Ray job does.

    The point is that the mover process never sees this table — the real job runs on the Ray cluster, so a
    test that fakes the SUBMIT but leaves the write in-process would not exercise the reconstruction at all.
    """
    upstream = lance.dataset(from_uri).to_table()
    out = upstream.append_column(pa.field("stage", pa.string()), pa.array([stage] * upstream.num_rows, pa.string()))
    lance.write_dataset(out, to_uri, mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)


def test_ray_branch_emits_column_edges_reconstructed_from_disk(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE production seam (MEDALLION_COMPUTE_ENABLED + MEDALLION_RAY_ENABLED): the transform runs on the
    Ray cluster, so handle_stage must still emit real columnLineage — parsed back out by the consumer."""
    raw, bronze = str(tmp_path / "raw"), str(tmp_path / "bronze")
    seed_raw(raw, {}, rows=4)  # columns [id, payload]
    settings = _mover_settings(raw, bronze).model_copy(update={"ray_enabled": True})

    async def fake_submit(_settings: Any, *, from_uri: str, to_uri: str, stage: str, token: str) -> None:
        _ray_job_write(from_uri, to_uri, stage)  # the cluster wrote it; this process only measures

    monkeypatch.setattr(mover, "submit_stage_job", fake_submit)
    dapr = _FakeDapr()

    result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t1"}}))
    assert result == {"status": "SUCCESS"}

    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic)
    output = Dataset.model_validate(lineage["data"]["outputs"][0])
    edges = {(e["out_field"], e["name"], e["field"], e["subtype"]) for e in output.column_edges}
    assert edges == {
        ("id", "raw_events", "id", "IDENTITY"),
        ("payload", "raw_events", "payload", "IDENTITY"),
    }
    # …and it is the REAL written version being described, not a fabricated one.
    facets = lineage["data"]["outputs"][0]["facets"]
    assert facets["version"]["datasetVersion"] == str(lance.dataset(bronze).version)
