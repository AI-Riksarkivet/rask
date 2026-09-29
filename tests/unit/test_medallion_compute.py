"""Unit tests for the fake-Ray medallion compute (#25 / P1 #6 seam) — real Lance, no S3/Dapr.

Drives the in-process Lance read→transform→write against a temp directory (Lance writes local paths with
empty ``storage_options``), then proves the stage runner/producer WIRING carries the REAL Lance version into the
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
from medallion.services.compute import seed_bronze, transform_stage
from medallion.services.produce import produce
from medallion.services.transform import handle_stage
from service_kit.lakehouse.quality import assert_quality, passed


# --------------------------------------------------------------------------- #
# the compute itself (real Lance read → transform → write)
# --------------------------------------------------------------------------- #


def test_the_version_advances_WITH_THE_DATA_and_not_otherwise(tmp_path: Any) -> None:
    """A version records a change. A re-run over unchanged rows is not one.

    THIS TEST ASSERTED THE OPPOSITE and the behaviour genuinely moved (§8 change 4), so it is
    inverted rather than deleted. It read "a re-run (overwrite) produces a NEW Lance version — so the
    emitted lineage advances with the data", and the clause after the dash is the property that
    actually mattered. It still holds, and is asserted below: when the DATA advances, the version
    advances with it.

    What no longer holds is the first clause. Dapr delivers at least once, so a stage runner re-runs a
    completed stage as a matter of routine; the old shape rewrote the entire tier to reproduce bytes
    already on disk, dropped the JSON index doing it (see `_index_lineage`), and committed a version
    that recorded nothing. An audit trail whose versions include "nothing happened, twice" is a worse
    audit trail, not a more thorough one.

    The alignment guard is what makes the no-op safe, and it is tested exhaustively in
    `services/medallion/tests/test_additive_stage_adds_columns.py`: anything but an element-wise
    `source_rowid` match falls back to the overwrite.
    """
    bronze, silver = str(tmp_path / "bronze"), str(tmp_path / "silver")
    seed_bronze(bronze, {}, rows=2)

    v1 = transform_stage(bronze, silver, {}, stage="silver").version
    v2 = transform_stage(bronze, silver, {}, stage="silver").version
    assert v2 == v1, "a redelivered trigger committed a version for a tier nothing had changed"

    # …and the half the original test was really protecting: real change still advances the version.
    seed_bronze(bronze, {}, rows=5)
    v3 = transform_stage(bronze, silver, {}, stage="silver").version
    assert v3 > v2, "the tier followed its upstream but did not advance its version"


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


def _stage_runner_settings(bronze: str, silver: str) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "from_uri": bronze,
            "to_uri": silver,
            "from_namespace": "bronze",
            "from_dataset": "bronze$events",
            "to_namespace": "silver",
            "to_dataset": "silver$features",
            "operation": "embed_features",
            "pub_topic": "silver.ready",
        }
    )


def test_handle_stage_writes_real_data_and_emits_the_real_version(tmp_path: Any) -> None:
    bronze, silver = str(tmp_path / "bronze"), str(tmp_path / "silver")
    seed_bronze(bronze, {}, rows=4)
    settings = _stage_runner_settings(bronze, silver)
    dapr = _FakeDapr()

    result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t1"}}))
    assert result == {"status": "SUCCESS"}

    # The downstream Lance dataset really exists with the rows + the stage stamp.
    out = lance.dataset(silver).to_table()
    assert out.num_rows == 4 and set(out.column("stage").to_pylist()) == {"silver"}

    # The emitted lineage event carries the REAL downstream version (not the hardcoded 1).
    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic and p["data"]["eventType"] != "START")
    output = lineage["data"]["outputs"][0]
    assert output["name"] == "silver$features"
    assert output["facets"]["version"]["datasetVersion"] == str(_newest_data_version(silver))
    # …and the standard outputStatistics facet carries the runtime-measured rows + on-disk bytes it wrote.
    stats = output["facets"]["outputStatistics"]
    assert stats["rowCount"] == 4
    assert stats["size"] > 0
    assert "OutputStatisticsOutputDatasetFacet" in stats["_schemaURL"]
    # …and the stage runner fired NO next-stage topic. This asserted the opposite while `GateOutcome.TRIGGER`
    # existed: the stage runner published `silver.ready` itself, promoting without the catalog ruling. That
    # was the second enforcement point `catalog/services/publication.py` exists to prevent, and it was
    # the DEFAULT path because MEDALLION_CASCADE_VIA_PUBLISH defaulted False.
    #
    # Inverted rather than deleted, because "the assertion went away" and "the door went away" read
    # identically in a diff, and only one of them is what happened.
    assert not any(p["topic"] == "silver.ready" for p in dapr.published), (
        "the stage runner fired the next stage itself — there is ONE door, and it is the catalog's tag move"
    )


def test_compute_off_emits_no_phantom_complete(tmp_path: Any) -> None:
    """A COMPLETE must never describe a dataset that was never written.

    ``chart/values.yaml`` defaults ``compute.enabled: false``, so this is the DEPLOYED path, not an
    edge case. With compute off the stage runner writes nothing, yet it emitted a COMPLETE whose output
    carried ``version: "1"`` — a measured property of a dataset that does not exist. A consumer of
    the graph cannot distinguish that from a real v1 write, which is the specific way lineage stops
    being evidence and becomes decoration.

    The contract asserted here is the one the FAIL path already uses (``events.py``: "A FAIL run
    produced no data: it keeps a BARE output (name only) and NO version/stats"): a run that produced
    no data describes no data, and says so in the ``lance`` facet so a consumer can filter provenance-
    only runs deliberately rather than by inferring it from a missing facet.
    """
    bronze, silver = str(tmp_path / "bronze"), str(tmp_path / "silver")
    seed_bronze(bronze, {}, rows=4)
    settings = _stage_runner_settings(bronze, silver)
    settings.compute_enabled = False
    dapr = _FakeDapr()

    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t1"}}))
    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic and p["data"]["eventType"] != "START")
    output = lineage["data"]["outputs"][0]

    # The run is still recorded — the producer's emit IS the cascade head, so suppression is not an
    # option — but it is marked, and the mark is machine-readable rather than a naming convention.
    assert lineage["data"]["run"]["facets"]["lance"]["synthetic"] is True
    # …and it claims NOTHING measured about a dataset it never wrote.
    assert "version" not in output.get("facets", {}), (
        f"COMPLETE claims a version for a dataset that was never written: {output.get('facets', {}).get('version')}"
    )
    assert "outputStatistics" not in output.get("facets", {})
    assert "dataSource" not in output.get("facets", {})


def test_produce_seeds_real_bronze_and_emits_its_version(tmp_path: Any) -> None:
    bronze = str(tmp_path / "bronze")
    settings = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": bronze})
    dapr = _FakeDapr()

    result = asyncio.run(produce(cast(DaprClient, dapr), settings, token="idem-test"))
    assert result["status"] == "produced"
    # bronze$events really exists, and the emitted lineage records its real version.
    assert lance.dataset(bronze).to_table().num_rows > 0
    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic and p["data"]["eventType"] != "START")
    assert lineage["data"]["outputs"][0]["facets"]["version"]["datasetVersion"] == str(lance.dataset(bronze).version)


def test_produce_idempotency_token_converges_retries(tmp_path: Any) -> None:
    """Skill rule pinned (retry needs an idempotency key): two produces REUSING the caller's token
    emit head events with the SAME runId — the graph MERGEs the duplicate instead of forking two
    unrelated cascades; without a token each call mints a fresh one (distinct runIds)."""
    bronze = str(tmp_path / "bronze")
    settings = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": bronze})
    dapr = _FakeDapr()

    first = asyncio.run(produce(cast(DaprClient, dapr), settings, token="retry-key-1"))
    second = asyncio.run(produce(cast(DaprClient, dapr), settings, token="retry-key-1"))
    assert first["token"] == second["token"] == "retry-key-1"  # the response echoes the caller's key
    events = [p for p in dapr.published if p["topic"] == settings.lineage_topic]
    assert events[0]["data"]["run"]["runId"] == events[1]["data"]["run"]["runId"]

    fresh = asyncio.run(produce(cast(DaprClient, dapr), settings, token="idem-test"))
    assert fresh["token"] not in ("retry-key-1", "")  # no key → fresh random token, distinct run
    third = [p for p in dapr.published if p["topic"] == settings.lineage_topic][2]
    assert third["data"]["run"]["runId"] != events[0]["data"]["run"]["runId"]


# --------------------------------------------------------------------------- #
# the quality gate — assertions on the produced data, and blocked promotion
# --------------------------------------------------------------------------- #


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


def _quality_stage_runner_settings(from_uri: str, to_uri: str) -> MedallionSettings:
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


def test_a_failed_assertion_is_RECORDED_by_the_stage_runner_and_RULED_ON_by_the_catalog(tmp_path: Any) -> None:
    """The stage runner measures; it does not rule. Both halves are asserted here.

    This test used to expect DROP: the stage runner ran `assert_quality` on its own write and blocked the
    promotion itself. That was the second enforcement point — it answered the same question the
    catalog answers, with different consequences, and a stage could refuse a version the catalog had
    never been asked about.

    Nothing is lost by moving the verdict, and that is the part worth being precise about. The
    stage runner's block only ever withheld the next-stage TRIGGER, and there is no trigger any more; the
    catalog's `publish` refusal is what withholds a promotion now, and it is asserted at the PUBLISH
    branch. This stage runner has no catalog at all, so there is no promotion to withhold — it acks.

    What must NOT be lost is the audit fact, and that is why `assert_quality` still runs: the failed
    `not_null` is stamped into the `dataQualityAssertions` facet on the run event, so the graph
    records what the data looked like at the hop even though the hop did not rule on it.
    """
    silver = str(tmp_path / "silver")
    lance.write_dataset(pa.table({"id": pa.array([1, None], pa.int64()), "p": ["a", "b"]}), silver, mode="overwrite")
    gold = str(tmp_path / "gold")
    settings = _quality_stage_runner_settings(silver, gold)
    dapr = _FakeDapr()

    result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t"}}))
    # Ungoverned: no catalog, so nothing can move a tag and redelivery cannot conjure one. ACK, not
    # RETRY — the ungoverned write already warned, loudly, where it happened.
    assert result == {"status": "SUCCESS"}

    # The audit fact survived the move.
    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic and p["data"]["eventType"] != "START")
    facet = lineage["data"]["outputs"][0]["facets"]["dataQualityAssertions"]
    assert any(a["assertion"] == "not_null" and a["success"] is False for a in facet["assertions"])
    # And no second door opened.
    assert not any(p["topic"] == "gold.ready" for p in dapr.published)


def _newest_data_version(uri: str) -> int:
    """The newest version at ``uri`` whose transaction CHANGED ROWS.

    A stage rebuilds the lineage JSON index after its write, and that commits a `CreateIndex` version of
    its own. The announced version must be the data commit beneath it — measured 2026-09-11, all 253
    stage-authored edges in the estate named the index build instead, so four datasets had no producer
    edge on any retained data version. Asserting against `ds.version` pinned that defect, because
    `ds.version` is "whatever the dataset is at now", not "where this run's data landed".
    """
    import lance

    ds = lance.dataset(uri)
    for entry in sorted(ds.versions(), key=lambda v: int(v["version"]), reverse=True):
        number = int(entry["version"])
        try:
            operation = type(getattr(ds.read_transaction(number), "operation", None)).__name__
        except Exception:  # noqa: BLE001 — an unreadable transaction is not evidence of maintenance
            return number
        if operation not in {"Rewrite", "CreateIndex", "UpdateConfig"}:
            return number
    raise AssertionError(f"{uri} has no data-operation version")
