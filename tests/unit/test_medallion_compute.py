"""Unit tests for the fake-Ray medallion compute (#25 / P1 #6 seam) — real Lance, no S3/Dapr.

Drives the in-process Lance read→transform→write against a temp directory (Lance writes local paths with
empty ``storage_options``), then proves the mover/producer WIRING carries the REAL Lance version into the
emitted OpenLineage event — i.e. with compute on, the event-driven cascade produces actual versioned data,
not just provenance. The async handlers are driven with stdlib ``asyncio.run`` (the project convention).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import lance
import pyarrow as pa
from dapr.aio.clients import DaprClient
from medallion.core.config import MedallionSettings
from medallion.services.compute import seed_raw, transform_stage
from medallion.services.produce import produce
from medallion.services.quality import assert_quality, passed
from medallion.services.transform import handle_stage


# --------------------------------------------------------------------------- #
# the compute itself (real Lance read → transform → write)
# --------------------------------------------------------------------------- #


def test_seed_raw_writes_a_real_dataset(tmp_path: Any) -> None:
    uri = str(tmp_path / "raw")
    result = seed_raw(uri, {}, rows=5)
    table = lance.dataset(uri).to_table()
    assert table.num_rows == 5
    assert table.column_names == ["id", "payload"]
    assert result.version == lance.dataset(uri).version  # the returned version == what lineage records
    # the measured output statistics are exact (not estimated): real rows + a positive on-disk byte count.
    assert result.row_count == 5
    assert result.size_bytes > 0


def test_transform_stage_measures_real_rows_and_bytes(tmp_path: Any) -> None:
    # The compute reports the runtime-measured output statistics the outputStatistics facet carries.
    raw, bronze = str(tmp_path / "raw"), str(tmp_path / "bronze")
    seed_raw(raw, {}, rows=6)
    result = transform_stage(raw, bronze, {}, stage="bronze")
    assert result.row_count == 6  # rows flowed forward, exactly
    assert result.size_bytes > 0  # the stage column adds bytes; the count is real, from the dataset stats


def test_transform_stage_carries_rows_forward_and_stamps_stage(tmp_path: Any) -> None:
    raw, bronze = str(tmp_path / "raw"), str(tmp_path / "bronze")
    seed_raw(raw, {}, rows=4)
    transform_stage(raw, bronze, {}, stage="bronze")
    out = lance.dataset(bronze).to_table()
    assert out.num_rows == 4  # real rows flowed forward
    assert set(out.column("stage").to_pylist()) == {"bronze"}  # provenance column stamped


def test_transform_stage_replaces_stage_column_not_duplicates_it(tmp_path: Any) -> None:
    # Across two hops the upstream is already stamped (bronze); the next hop must SET the column to silver,
    # not append a second `stage` column (which would collide on the name).
    raw, bronze, silver = (str(tmp_path / n) for n in ("raw", "bronze", "silver"))
    seed_raw(raw, {}, rows=3)
    transform_stage(raw, bronze, {}, stage="bronze")
    transform_stage(bronze, silver, {}, stage="silver")
    out = lance.dataset(silver).to_table()
    assert out.column_names.count("stage") == 1
    assert set(out.column("stage").to_pylist()) == {"silver"}


def test_transform_stage_rerun_bumps_the_version(tmp_path: Any) -> None:
    # A re-run (overwrite) produces a NEW Lance version — so the emitted lineage advances with the data.
    raw, bronze = str(tmp_path / "raw"), str(tmp_path / "bronze")
    seed_raw(raw, {}, rows=2)
    v1 = transform_stage(raw, bronze, {}, stage="bronze").version
    v2 = transform_stage(raw, bronze, {}, stage="bronze").version
    assert v2 > v1


def test_storage_options_empty_for_local_and_s3_when_configured() -> None:
    assert MedallionSettings.model_validate({"compute_enabled": True}).storage_options() == {}
    s3 = MedallionSettings.model_validate({"s3_endpoint": "http://rustfs:9000", "s3_access_key_id": "k", "s3_secret_access_key": "s"})
    assert s3.storage_options()["endpoint"] == "http://rustfs:9000"
    assert s3.storage_options()["allow_http"] == "true"


# --------------------------------------------------------------------------- #
# the WIRING — the cascade carries the real version into the emitted lineage
# --------------------------------------------------------------------------- #


class _FakeDapr:
    """Captures published events (the lineage emit + the next-stage trigger)."""

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


def test_handle_stage_writes_real_data_and_emits_the_real_version(tmp_path: Any) -> None:
    raw, bronze = str(tmp_path / "raw"), str(tmp_path / "bronze")
    seed_raw(raw, {}, rows=4)
    settings = _mover_settings(raw, bronze)
    dapr = _FakeDapr()

    result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t1"}}))
    assert result == {"status": "SUCCESS"}

    # The downstream Lance dataset really exists with the rows + the stage stamp.
    out = lance.dataset(bronze).to_table()
    assert out.num_rows == 4 and set(out.column("stage").to_pylist()) == {"bronze"}

    # The emitted lineage event carries the REAL downstream version (not the hardcoded 1).
    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic)
    output = lineage["data"]["outputs"][0]
    assert output["name"] == "bronze$events"
    assert output["facets"]["version"]["datasetVersion"] == str(lance.dataset(bronze).version)
    # …and the standard outputStatistics facet carries the runtime-measured rows + on-disk bytes it wrote.
    stats = output["facets"]["outputStatistics"]
    assert stats["rowCount"] == 4
    assert stats["size"] > 0
    assert "OutputStatisticsOutputDatasetFacet" in stats["_schemaURL"]
    # …and the next-stage trigger fired so the cascade continues.
    assert any(p["topic"] == "bronze.ready" for p in dapr.published)


def test_handle_stage_compute_off_writes_no_data_and_emits_version_one(tmp_path: Any) -> None:
    # The gate: with compute OFF the mover stays a dummy-emitter — no downstream dataset, version 1.
    raw, bronze = str(tmp_path / "raw"), str(tmp_path / "bronze")
    seed_raw(raw, {}, rows=4)
    settings = _mover_settings(raw, bronze)
    settings.compute_enabled = False
    dapr = _FakeDapr()

    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t1"}}))
    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic)
    output = lineage["data"]["outputs"][0]
    assert output["facets"]["version"]["datasetVersion"] == "1"
    # …and a dummy emit measured nothing, so it must NOT claim an outputStatistics facet.
    assert "outputStatistics" not in output["facets"]
    # No write happened — opening the downstream path as a dataset must fail (it was never created).
    try:
        lance.dataset(bronze)
        raise AssertionError("downstream dataset must not exist when compute is off")
    except ValueError:
        pass


def test_produce_seeds_real_raw_and_emits_its_version(tmp_path: Any) -> None:
    raw = str(tmp_path / "raw")
    settings = MedallionSettings.model_validate({"compute_enabled": True, "raw_uri": raw, "raw_namespace": "raw", "raw_dataset": "raw_events"})
    dapr = _FakeDapr()

    result = asyncio.run(produce(cast(DaprClient, dapr), settings))
    assert result["status"] == "produced"
    # raw_events really exists, and the emitted lineage records its real version.
    assert lance.dataset(raw).to_table().num_rows > 0
    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic)
    assert lineage["data"]["outputs"][0]["facets"]["version"]["datasetVersion"] == str(lance.dataset(raw).version)


def test_produce_idempotency_token_converges_retries(tmp_path: Any) -> None:
    """Skill rule pinned (retry needs an idempotency key): two produces REUSING the caller's token
    emit head events with the SAME runId — the graph MERGEs the duplicate instead of forking two
    unrelated cascades; without a token each call mints a fresh one (distinct runIds)."""
    raw = str(tmp_path / "raw")
    settings = MedallionSettings.model_validate({"compute_enabled": True, "raw_uri": raw, "raw_namespace": "raw", "raw_dataset": "raw_events"})
    dapr = _FakeDapr()

    first = asyncio.run(produce(cast(DaprClient, dapr), settings, token="retry-key-1"))
    second = asyncio.run(produce(cast(DaprClient, dapr), settings, token="retry-key-1"))
    assert first["token"] == second["token"] == "retry-key-1"  # the response echoes the caller's key
    events = [p for p in dapr.published if p["topic"] == settings.lineage_topic]
    assert events[0]["data"]["run"]["runId"] == events[1]["data"]["run"]["runId"]

    fresh = asyncio.run(produce(cast(DaprClient, dapr), settings))
    assert fresh["token"] not in ("retry-key-1", "")  # no key → fresh random token, distinct run
    third = [p for p in dapr.published if p["topic"] == settings.lineage_topic][2]
    assert third["data"]["run"]["runId"] != events[0]["data"]["run"]["runId"]


# --------------------------------------------------------------------------- #
# the quality gate — assertions on the produced data, and blocked promotion
# --------------------------------------------------------------------------- #


def test_assert_quality_passes_on_clean_data(tmp_path: Any) -> None:
    uri = str(tmp_path / "gold")
    seed_raw(uri, {}, rows=3)  # ids 0..2, no nulls
    checks = assert_quality(uri, {}, key_column="id")
    assert {c.assertion for c in checks} == {"row_count_positive", "not_null"}
    assert passed(checks)


def test_assert_quality_fails_on_null_key(tmp_path: Any) -> None:
    uri = str(tmp_path / "gold")
    lance.write_dataset(pa.table({"id": pa.array([1, None, 3], pa.int64()), "p": ["a", "b", "c"]}), uri, mode="overwrite")
    checks = assert_quality(uri, {}, key_column="id")
    not_null = next(c for c in checks if c.assertion == "not_null")
    assert not_null.success is False and not_null.column == "id"
    assert not passed(checks)


def test_assert_quality_fails_on_empty_dataset(tmp_path: Any) -> None:
    uri = str(tmp_path / "gold")
    lance.write_dataset(pa.table({"id": pa.array([], pa.int64())}), uri, mode="overwrite")
    checks = assert_quality(uri, {}, key_column="id")
    assert next(c for c in checks if c.assertion == "row_count_positive").success is False
    assert not passed(checks)


def test_assert_quality_skips_null_check_when_key_absent(tmp_path: Any) -> None:
    # A different stage may not carry the key column — skip the null check, don't fail it.
    uri = str(tmp_path / "gold")
    seed_raw(uri, {}, rows=2)
    checks = assert_quality(uri, {}, key_column="nonexistent")
    assert [c.assertion for c in checks] == ["row_count_positive"]


def test_assert_quality_declared_columns_block_breaking_changes(tmp_path: Any) -> None:
    """THE breaking-change detector (data-contract gap #1): a version whose schema no longer carries
    a DECLARED consumer dependency fails the gate (promotion blocked), while additive evolution and
    undeclared datasets stay untouched. seed_raw writes columns [id, payload]."""
    uri = str(tmp_path / "gold")
    seed_raw(uri, {}, rows=2)

    healthy = assert_quality(uri, {}, key_column="id", required_columns=["id", "payload"])
    declared = [c for c in healthy if c.assertion == "column_declared"]
    assert [(c.column, c.success) for c in declared] == [("id", True), ("payload", True)]
    assert passed(healthy)

    # The producer "renamed" payload → a declared dependency is gone: the SPECIFIC column is named,
    # the gate fails, and the untouched declaration still reports success (precise blame).
    breaking = assert_quality(uri, {}, key_column="id", required_columns=["id", "embedding"])
    by_column = {c.column: c.success for c in breaking if c.assertion == "column_declared"}
    assert by_column == {"id": True, "embedding": False}
    assert not passed(breaking)  # promotion blocked

    # No declaration (the default) → no new assertion, byte-identical to the pre-existing gate.
    undeclared = assert_quality(uri, {}, key_column="id")
    assert all(c.assertion != "column_declared" for c in undeclared)


def _write_blob_dataset(uri: str, payloads: list, *, base: str | None = None) -> None:
    from lance import blob_field

    schema = pa.schema([pa.field("id", pa.int64()), blob_field("media")])
    table = pa.table({"id": list(range(len(payloads))), "media": lance.blob_array(payloads)}, schema=schema)
    lance.write_dataset(
        table,
        uri,
        data_storage_version="2.2",
        initial_bases=[lance.DatasetBasePath(base, is_dataset_root=False)] if base else None,
    )


def test_assert_quality_blob_resolves_on_healthy_payloads(tmp_path: Any) -> None:
    # §9 P2: a blob column adds a blob_resolves assertion (column named); managed payloads —
    # including a zero-length/null row, which resolves trivially — pass. A tabular dataset
    # (the tests above) never grows the assertion, so this pins the skip direction too.
    uri = str(tmp_path / "gold")
    _write_blob_dataset(uri, [b"img-bytes" * 10, None, b""])
    checks = assert_quality(uri, {}, key_column="id")
    blob = next(c for c in checks if c.assertion == "blob_resolves")
    assert blob.success is True and blob.column == "media"
    assert passed(checks)


def test_assert_quality_blob_fails_on_dangling_external_pointer(tmp_path: Any) -> None:
    # THE case the assertion exists for (bucket wipe / wrong base): an external Blob.from_uri
    # object deleted from under the table passes every tabular check — count_rows and the null
    # filter never touch payload bytes — and would fail only at first read, far downstream of the
    # promotion. The gate must catch it AT promotion: probed 2026-07-12, a dangling pointer raises
    # from take_blobs itself (and size() alone would NOT catch it — it reads only the descriptor).
    ext = tmp_path / "ext"
    ext.mkdir()
    obj = ext / "obj.bin"
    obj.write_bytes(b"external-payload")
    uri = str(tmp_path / "gold")
    _write_blob_dataset(uri, [lance.Blob.from_uri(str(obj))], base=str(ext))

    healthy = assert_quality(uri, {}, key_column="id")
    assert next(c for c in healthy if c.assertion == "blob_resolves").success is True

    obj.unlink()
    checks = assert_quality(uri, {}, key_column="id")
    blob = next(c for c in checks if c.assertion == "blob_resolves")
    assert blob.success is False and blob.column == "media"
    assert not passed(checks)  # the gate blocks the promotion


def _quality_mover_settings(from_uri: str, to_uri: str) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "quality_enabled": True,
            "quality_key_column": "id",
            "from_uri": from_uri,
            "to_uri": to_uri,
            "from_namespace": "silver",
            "from_dataset": "silver$features",
            "to_namespace": "gold",
            "to_dataset": "gold$ml",
            "operation": "aggregate_gold",
            "pub_topic": "gold.ready",
        }
    )


def test_quality_gate_blocks_promotion_on_failed_assertion(tmp_path: Any) -> None:
    # Upstream carries a null id; the gate fails not_null → DROP, run recorded, next stage NOT triggered.
    silver = str(tmp_path / "silver")
    lance.write_dataset(pa.table({"id": pa.array([1, None], pa.int64()), "p": ["a", "b"]}), silver, mode="overwrite")
    gold = str(tmp_path / "gold")
    settings = _quality_mover_settings(silver, gold)
    dapr = _FakeDapr()

    result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t"}}))
    assert result == {"status": "DROP"}  # quality-blocked

    # The lineage WAS emitted with the failed assertion (auditable) — but the next-stage trigger did NOT.
    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic)
    facet = lineage["data"]["outputs"][0]["facets"]["dataQualityAssertions"]
    assert any(a["assertion"] == "not_null" and a["success"] is False for a in facet["assertions"])
    assert not any(p["topic"] == "gold.ready" for p in dapr.published)


def test_quality_gate_promotes_on_clean_data(tmp_path: Any) -> None:
    silver = str(tmp_path / "silver")
    seed_raw(silver, {}, rows=4)  # clean ids 0..3
    gold = str(tmp_path / "gold")
    settings = _quality_mover_settings(silver, gold)
    dapr = _FakeDapr()

    result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t"}}))
    assert result == {"status": "SUCCESS"}

    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic)
    facet = lineage["data"]["outputs"][0]["facets"]["dataQualityAssertions"]
    assert all(a["success"] for a in facet["assertions"])
    assert any(p["topic"] == "gold.ready" for p in dapr.published)  # promoted


def test_quality_off_emits_no_assertions_facet(tmp_path: Any) -> None:
    # compute ON but quality OFF: a real write + outputStatistics, but NO dataQualityAssertions facet.
    raw, bronze = str(tmp_path / "raw"), str(tmp_path / "bronze")
    seed_raw(raw, {}, rows=3)
    settings = _mover_settings(raw, bronze)  # compute on, quality off
    dapr = _FakeDapr()
    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t"}}))
    output = next(p for p in dapr.published if p["topic"] == settings.lineage_topic)["data"]["outputs"][0]
    assert "outputStatistics" in output["facets"]
    assert "dataQualityAssertions" not in output["facets"]
