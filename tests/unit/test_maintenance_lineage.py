"""Unit tests for compaction → lineage emission (#7b + §4 failure visibility) — infra-free.

Drives the async emit with stdlib ``asyncio.run`` (the project convention). Covers:
* the table-id parse from the catalog's ``<uuid>_<table_id>`` layout (incl. the boundaries),
* the OpenLineage maintenance-event shapes (COMPLETE and FAIL) **round-tripped through the lineage
  ``RunEvent`` model** — the cross-service wire contract,
* which datasets in a sweep get which event (material work → COMPLETE; ``maintain:``-errored → FAIL;
  ``open:``-errored / no-op / unparseable → nothing) + the parent-namespace derivation,
* the FAIL flood guard (deterministic per-dataset run id; COMPLETE keeps uuid4), the per-tick FAIL cap
  (logged, never silent) and the concurrent (bounded) FAIL fan-out,
* the emitter factory (no-op when off/unwired; Dapr when wired) and the best-effort publish (a broker
  outage must never fail a sweep).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, cast

import pytest

from lineage.models import RunEvent
from maintenance.core.lineage_emit import (
    COMPACTION,
    DaprMaintenanceEmitter,
    NoopEmitter,
    build_maintenance_event,
    build_maintenance_fail_event,
    classify_retryable,
    make_emitter,
    table_id_from_uri,
)
from maintenance.services.optimize import DatasetResult
from maintenance.services.sweep import _MAX_FAIL_EMITS_PER_TICK, emit_sweep_lineage


# --------------------------------------------------------------------------- #
# table_id parse from the catalog's <uuid>_<table_id> dataset layout
# --------------------------------------------------------------------------- #


def test_table_id_from_uri_splits_on_first_underscore() -> None:
    # The catalog lays a table out as s3://<bucket>/<uuid>_<table_id>; the id may itself contain '$'.
    assert table_id_from_uri("s3://lance-catalog/abcd_ns$table") == "ns$table"


def test_table_id_from_uri_keeps_later_underscores_in_id() -> None:
    # Only the FIRST '_' separates the uuid from the id — an id containing '_' survives intact.
    assert table_id_from_uri("s3://lance-catalog/uuid_my_table") == "my_table"


def test_table_id_from_uri_tolerates_trailing_slash() -> None:
    assert table_id_from_uri("s3://lance-catalog/abcd_gold$catalog/") == "gold$catalog"


def test_table_id_from_uri_none_without_underscore() -> None:
    # A directory that isn't the <uuid>_<id> layout yields no id → no bogus maintenance event.
    assert table_id_from_uri("s3://lance-catalog/manifestlike") is None


# --------------------------------------------------------------------------- #
# maintenance event shape + the cross-service wire contract
# --------------------------------------------------------------------------- #


def test_build_maintenance_event_shape() -> None:
    event = build_maintenance_event(
        table_id="ns$table",
        namespace="ns",
        job_namespace="compaction",
        run_id="r1",
        event_time="2026-06-30T00:00:00Z",
    )
    assert event["run"]["facets"]["lance"]["operation"] == COMPACTION
    assert event["outputs"] == [{"namespace": "ns", "name": "ns$table"}]
    assert event["inputs"] == []
    # Versionless: a maintenance pass asserts no data version, so no output facets at all.
    assert "facets" not in event["outputs"][0]


def test_producer_uri_names_this_repo_and_this_service() -> None:
    # The spec-required OpenLineage ``producer`` is wire-visible on every RunEvent. It must resolve —
    # the repo is AI-Riksarkivet/rask and the code lives in services/maintenance, not the dissolved
    # Borg93/lance-ns services/compaction path (which now 404s).
    event = build_maintenance_event(
        table_id="ns$table",
        namespace="ns",
        job_namespace="compaction",
        run_id="r1",
        event_time="2026-06-30T00:00:00Z",
    )
    producer = event["producer"]
    assert "lance-ns" not in producer, producer
    assert "services/compaction" not in producer, producer
    assert "AI-Riksarkivet/rask" in producer, producer
    assert "services/maintenance" in producer, producer


def test_maintenance_event_round_trips_through_lineage_run_event() -> None:
    # The wire contract: the lineage service must parse a compaction event as a successful, versionless,
    # input-less run on the dataset — so it records a WROTE with no version + no DERIVED_FROM (no inputs).
    event = build_maintenance_event(
        table_id="silver$features",
        namespace="silver",
        job_namespace="compaction",
        run_id="r2",
        event_time="2026-06-30T00:00:00Z",
    )
    parsed = RunEvent.model_validate(event)
    assert parsed.is_success
    assert parsed.operation == COMPACTION
    assert [d.name for d in parsed.outputs] == ["silver$features"]
    assert parsed.inputs == []
    assert parsed.output_version("silver$features") is None  # versionless on the WROTE edge


# --------------------------------------------------------------------------- #
# FAIL event shape + the cross-service wire contract (§4 failure visibility)
# --------------------------------------------------------------------------- #


def _fail_event(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "table_id": "ns$table",
        "namespace": "ns",
        "job_namespace": "compaction",
        "run_id": "r-fail",
        "event_time": "2026-07-10T00:00:00Z",
        "error": "maintain: Commit conflict for version 42",
    }
    kwargs.update(overrides)
    return build_maintenance_fail_event(**kwargs)


def test_build_maintenance_fail_event_shape() -> None:
    # eventType FAIL, bare output (name only), errorMessage facet with message + programmingLanguage,
    # and NO version/schema/statistics facets — a failed maintenance pass must never fabricate lineage.
    event = _fail_event()
    assert event["eventType"] == "FAIL"
    assert event["outputs"] == [{"namespace": "ns", "name": "ns$table"}]  # bare: no facets key at all
    assert event["inputs"] == []
    assert event["job"]["name"] == f"{COMPACTION}.ns$table"  # same job identity as the COMPLETE event
    error_facet = event["run"]["facets"]["errorMessage"]
    assert error_facet["message"] == "maintain: Commit conflict for version 42"
    assert error_facet["programmingLanguage"] == "PYTHON"
    assert "_producer" in error_facet and "_schemaURL" in error_facet
    assert event["run"]["facets"]["lance"]["operation"] == COMPACTION


def test_fail_event_round_trips_through_lineage_run_event() -> None:
    # The wire contract: lineage parses it as a FAILED versionless run on the dataset — the repo then
    # makes a bare WROTE edge (producers() surfaces the attempt) and, since it is not a success, never
    # a version or a DERIVED_FROM.
    parsed = RunEvent.model_validate(_fail_event())
    assert not parsed.is_success
    assert parsed.operation == COMPACTION
    assert [d.name for d in parsed.outputs] == ["ns$table"]
    assert parsed.output_version("ns$table") is None
    assert parsed.error_message == "maintain: Commit conflict for version 42"


def test_fail_event_caps_the_error_message() -> None:
    # Exception strings embed s3:// URIs and Rust backtraces — the facet message is capped so a failing
    # dataset can't bloat every FAIL event.
    event = _fail_event(error="maintain: " + "x" * 5000)
    assert len(event["run"]["facets"]["errorMessage"]["message"]) == 1000


def test_fail_event_retryable_is_best_effort() -> None:
    # String heuristic only (pylance 8.0.0 raises no typed conflict exception): a confident match rides
    # the facet as a custom field; anything else omits the key rather than guessing.
    assert classify_retryable("maintain: Commit conflict for version 3") is True
    assert classify_retryable("maintain: retryable commit failure") is True
    assert classify_retryable("maintain: incompatible schema change") is False
    assert classify_retryable("maintain: NoSuchKey: data file gone") is None
    # NEGATED forms are the review's inversion trap: real messages say "not retryable" / "retry limit
    # exceeded" — a bare substring match on "retry" would stamp exactly those terminal errors True.
    assert classify_retryable("maintain: operation is non-retryable") is False
    assert classify_retryable("maintain: commit failed: not retryable") is False
    assert classify_retryable("maintain: retries exhausted after 3 attempts") is False
    assert classify_retryable("maintain: retry limit exceeded") is False
    assert _fail_event(error="maintain: Commit conflict")["run"]["facets"]["errorMessage"]["retryable"] is True
    assert "retryable" not in _fail_event(error="maintain: NoSuchKey")["run"]["facets"]["errorMessage"]


# --------------------------------------------------------------------------- #
# per-sweep emit selection
# --------------------------------------------------------------------------- #


class _RecordingEmitter:
    """Captures (table_id, namespace) per COMPLETE emit and (table_id, namespace, error) per FAIL emit."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failed_calls: list[tuple[str, str, str]] = []

    async def emit_maintenance(self, *, table_id: str, namespace: str) -> None:
        self.calls.append((table_id, namespace))

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str) -> None:
        self.failed_calls.append((table_id, namespace, error))


def _result(uri: str, **kw: Any) -> DatasetResult:
    return DatasetResult(uri=uri, **kw)


def test_emit_sweep_lineage_emits_only_for_materially_compacted_datasets() -> None:
    emitter = _RecordingEmitter()
    results = [
        _result("s3://b/u1_ns$a", fragments_removed=3),  # compacted → emit
        _result("s3://b/u2_ns$b", old_versions_removed=2),  # GC'd → emit
        _result("s3://b/u3_ns$c"),  # no-op tick → skip
        _result("s3://b/u4_ns$d", fragments_removed=1, error="open: boom"),  # transient noise → skip
        _result("s3://b/nounderscore", fragments_removed=1),  # unparseable id → skip
    ]
    asyncio.run(emit_sweep_lineage(cast(Any, emitter), results, delimiter="$"))
    assert emitter.calls == [("ns$a", "ns"), ("ns$b", "ns")]
    assert emitter.failed_calls == []  # an open:-errored dir NEVER produces an event (§4 selection)


def test_emit_sweep_lineage_fails_only_for_maintain_errors() -> None:
    # §4 selection: maintain: (escaped compact/GC — post-auto-retry terminal) → FAIL; open: (unreadable /
    # declared-only dir) → nothing; a maintain:-errored dataset that ALSO did partial material work gets
    # the FAIL, not a COMPLETE (the pass did not complete); unparseable layout → nothing either way
    # (the documented medallion-nested blind spot).
    emitter = _RecordingEmitter()
    results = [
        _result("s3://b/u1_ns$a", error="maintain: Commit conflict"),
        _result("s3://b/u2_ns$b", fragments_removed=2, error="maintain: cleanup blew up"),  # partial work
        _result("s3://b/u3_ns$c", error="open: not a dataset"),
        _result("s3://b/u4_ns$d", error="boom"),  # UNPREFIXED error → neither event (selection contract)
        _result("s3://b/medallion", error="maintain: nope"),  # no <uuid>_<id> layout → no id → skip
        _result("s3://b/u5_ns$e", fragments_removed=1),  # healthy → COMPLETE as before
    ]
    asyncio.run(emit_sweep_lineage(cast(Any, emitter), results, delimiter="$"))
    assert emitter.calls == [("ns$e", "ns")]
    assert emitter.failed_calls == [
        ("ns$a", "ns", "maintain: Commit conflict"),
        ("ns$b", "ns", "maintain: cleanup blew up"),
    ]


def test_emit_sweep_lineage_caps_fail_emits_and_logs_the_drop(caplog: pytest.LogCaptureFixture) -> None:
    # The per-tick FAIL fan-out is capped so a bucket where everything fails can't push the cron handler
    # past the 30s Dapr ack window — and the cap is LOGGED, never silent (dropped datasets converge onto
    # the same deterministic run ids next tick anyway).
    emitter = _RecordingEmitter()
    results = [_result(f"s3://b/u{i}_ns$t{i}", error="maintain: boom") for i in range(_MAX_FAIL_EMITS_PER_TICK + 5)]
    with caplog.at_level("WARNING"):
        asyncio.run(emit_sweep_lineage(cast(Any, emitter), results, delimiter="$"))
    assert len(emitter.failed_calls) == _MAX_FAIL_EMITS_PER_TICK
    assert any(r.message == "maintenance_fail_emits_capped" for r in caplog.records)


def test_cap_counts_actual_emits_not_unparseable_slots(caplog: pytest.LogCaptureFixture) -> None:
    # Review 2026-07-10: the cap must apply AFTER the layout filter — 30 unparseable maintain: failures
    # (the medallion-nested blind spot) must not consume cap slots and starve the 5 real emits, and no
    # cap warning fires when the actual emit count is under the cap.
    emitter = _RecordingEmitter()
    results = [_result("s3://b/medallion", error="maintain: nope")] * 30 + [_result(f"s3://b/u{i}_ns$t{i}", error="maintain: real") for i in range(5)]
    with caplog.at_level("WARNING"):
        asyncio.run(emit_sweep_lineage(cast(Any, emitter), results, delimiter="$"))
    assert len(emitter.failed_calls) == 5  # every parseable failure emitted
    assert not any(r.message == "maintenance_fail_emits_capped" for r in caplog.records)


def test_emit_sweep_lineage_never_raises_even_for_a_broken_emitter() -> None:
    # The guardrail is structural: "a publish failure must never fail the sweep" holds even for an
    # emitter that predates the FAIL protocol (AttributeError while building the coros) or one whose
    # coroutine raises — either way the cron handler must never see it.
    class _OldProtocolEmitter:
        async def emit_maintenance(self, *, table_id: str, namespace: str) -> None:
            return None

    class _RaisingEmitter(_RecordingEmitter):
        async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str) -> None:
            raise RuntimeError("boom from a non-best-effort emitter")

    results = [_result("s3://b/u1_ns$a", error="maintain: x")]
    asyncio.run(emit_sweep_lineage(cast(Any, _OldProtocolEmitter()), results, delimiter="$"))  # no raise
    asyncio.run(emit_sweep_lineage(cast(Any, _RaisingEmitter()), results, delimiter="$"))  # no raise


def test_fail_batch_bounded_through_the_real_emitter_with_a_hung_sidecar() -> None:
    # The COMPOSED ack-window claim: cap + concurrent gather + the real DaprMaintenanceEmitter._publish
    # timeout. 30 failing datasets against a sidecar that never responds must complete in ~one publish
    # timeout (0.2s here), not 30 × anything — and must not raise.
    class _HungClient:
        async def publish_event(self, **_kw: Any) -> None:
            await asyncio.sleep(60)

    emitter = DaprMaintenanceEmitter(cast(Any, _HungClient()), pubsub="p", topic="t", job_namespace="compaction", timeout_seconds=0.2)
    results = [_result(f"s3://b/u{i}_ns$t{i}", error="maintain: hung") for i in range(30)]
    start = time.monotonic()
    asyncio.run(emit_sweep_lineage(cast(Any, emitter), results, delimiter="$"))
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"hung-sidecar FAIL batch not bounded ({elapsed:.2f}s)"


def test_emit_sweep_lineage_fail_batch_is_concurrent_not_sequential() -> None:
    # The FAIL publishes gather CONCURRENTLY (each internally bounded by the emitter's publish timeout):
    # N slow publishes must take ~one publish's latency, not N of them — the ack-window bound. 20 emits
    # sleeping 0.1s each would be 2s sequential; concurrent they finish in a fraction of that.
    class _SlowEmitter(_RecordingEmitter):
        async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str) -> None:
            await asyncio.sleep(0.1)
            await super().emit_maintenance_failed(table_id=table_id, namespace=namespace, error=error)

    emitter = _SlowEmitter()
    results = [_result(f"s3://b/u{i}_ns$t{i}", error="maintain: slow") for i in range(20)]
    start = time.monotonic()
    asyncio.run(emit_sweep_lineage(cast(Any, emitter), results, delimiter="$"))
    elapsed = time.monotonic() - start
    assert len(emitter.failed_calls) == 20
    assert elapsed < 1.0, f"FAIL batch ran sequentially ({elapsed:.2f}s for 20 × 0.1s sleeps)"


def test_emit_sweep_lineage_root_table_has_empty_namespace() -> None:
    # A single-segment table id has no parent namespace → "" (matching the catalog's create emit, so the
    # maintenance event never clobbers the dataset node's namespace).
    emitter = _RecordingEmitter()
    asyncio.run(emit_sweep_lineage(cast(Any, emitter), [_result("s3://b/uuid_solo", fragments_removed=1)], delimiter="$"))
    assert emitter.calls == [("solo", "")]


# --------------------------------------------------------------------------- #
# emitter factory + transport behavior
# --------------------------------------------------------------------------- #


def test_make_emitter_noop_when_disabled() -> None:
    emitter = make_emitter(enabled=False, dapr=None, pubsub="p", topic="t", job_namespace="compaction")
    assert isinstance(emitter, NoopEmitter)


def test_make_emitter_noop_when_enabled_but_unwired() -> None:
    # Enabled but no Dapr client → stay a no-op rather than silently publish nowhere (fail safe).
    emitter = make_emitter(enabled=True, dapr=None, pubsub="p", topic="t", job_namespace="compaction")
    assert isinstance(emitter, NoopEmitter)


def test_make_emitter_dapr_when_enabled_and_wired() -> None:
    emitter = make_emitter(
        enabled=True,
        dapr=cast(Any, object()),
        pubsub="lineage-pubsub",
        topic="lineage.events.v1",
        job_namespace="compaction",
    )
    assert isinstance(emitter, DaprMaintenanceEmitter)


def test_noop_emitter_does_nothing() -> None:
    asyncio.run(NoopEmitter().emit_maintenance(table_id="ns$a", namespace="ns"))  # no raise == pass


class _FakeDaprClient:
    def __init__(self) -> None:
        self.published: list[dict[str, str]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"pubsub": pubsub_name, "topic": topic_name, "data": data})


def test_dapr_emitter_publishes_to_configured_pubsub_and_topic() -> None:
    client = _FakeDaprClient()
    emitter = DaprMaintenanceEmitter(
        cast(Any, client),
        pubsub="lineage-pubsub",
        topic="lineage.events.v1",
        job_namespace="compaction",
        timeout_seconds=5.0,
    )
    asyncio.run(emitter.emit_maintenance(table_id="ns$a", namespace="ns"))
    assert len(client.published) == 1
    assert client.published[0]["pubsub"] == "lineage-pubsub"
    assert client.published[0]["topic"] == "lineage.events.v1"
    payload = json.loads(client.published[0]["data"])
    assert payload["outputs"][0]["name"] == "ns$a"
    assert payload["run"]["facets"]["lance"]["operation"] == COMPACTION


def test_dapr_emitter_best_effort_swallows_publish_failure() -> None:
    # A sidecar/broker outage must NEVER fail a maintenance sweep — the publish is best-effort.
    class _BoomClient:
        async def publish_event(self, **_kw: Any) -> None:
            raise RuntimeError("sidecar down")

    emitter = DaprMaintenanceEmitter(cast(Any, _BoomClient()), pubsub="p", topic="t", job_namespace="compaction", timeout_seconds=5.0)
    asyncio.run(emitter.emit_maintenance(table_id="ns$a", namespace="ns"))  # no raise == pass
    asyncio.run(  # the FAIL path is best-effort the same way
        emitter.emit_maintenance_failed(table_id="ns$a", namespace="ns", error="maintain: x")
    )


def test_fail_run_id_is_deterministic_per_dataset_and_complete_stays_random() -> None:
    # The flood guard (§4): every tick's FAIL for the same dataset carries the SAME run id, so the graph
    # MERGEs one (:Run) node and /events dedups the redelivered terminal — while COMPLETE keeps uuid4
    # (each materially-compacting tick IS a distinct run; §4 says do NOT change that).
    client = _FakeDaprClient()
    emitter = DaprMaintenanceEmitter(cast(Any, client), pubsub="p", topic="t", job_namespace="compaction", timeout_seconds=5.0)

    async def drive() -> None:
        await emitter.emit_maintenance_failed(table_id="ns$a", namespace="ns", error="maintain: t1")
        await emitter.emit_maintenance_failed(table_id="ns$a", namespace="ns", error="maintain: t2")
        await emitter.emit_maintenance_failed(table_id="ns$b", namespace="ns", error="maintain: t1")
        await emitter.emit_maintenance(table_id="ns$a", namespace="ns")
        await emitter.emit_maintenance(table_id="ns$a", namespace="ns")

    asyncio.run(drive())
    run_ids = [json.loads(p["data"])["run"]["runId"] for p in client.published]
    fail_a1, fail_a2, fail_b, complete_1, complete_2 = run_ids
    assert fail_a1 == fail_a2  # two ticks, one dataset → ONE Run node
    assert fail_b != fail_a1  # per-dataset, not global
    assert complete_1 != complete_2  # COMPLETE semantics untouched (uuid4 per tick)
    assert complete_1 not in {fail_a1, fail_b}


def test_a_DECLARED_lineage_name_is_read_from_the_dataset(tmp_path: Any) -> None:
    """The medallion tiers cannot be NAMED from their URI, so a producer must be able to declare it.

    `table_id_from_uri` splits the directory on its first "_", which works for the catalog's
    `<uuid8>_<ns>$<table>` layout and returns None for `medallion/<tier>`. That is not a parsing
    shortfall: the chart composes those URIs from the namespace ALONE while the canonical id is a
    separate literal, so `medallion/bronze` is both `bronze$events` and `bronze$pages`. No split can
    recover a name the path never carried.

    The name must equal the OpenFGA object id — delivery re-checks `can_get_metadata` against
    `table:<output name>` — so a wrong name counts every recipient HIDDEN, which is worse than silence.
    Hence a declared key rather than a guess.
    """
    import lance
    import pyarrow as pa

    from maintenance.core.lineage_emit import LINEAGE_DATASET_ID_KEY, declared_table_id

    uri = str(tmp_path / "medallion-bronze.lance")
    table = pa.table({"v": [1, 2, 3]})
    table = table.replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "bronze$events"})
    lance.write_dataset(table, uri)

    assert declared_table_id(lance.dataset(uri)) == "bronze$events"


def test_an_UNDECLARED_dataset_yields_None_rather_than_a_guess(tmp_path: Any) -> None:
    """No key means no name. Falling back to a guess is the failure this exists to avoid — and datasets
    already on disk carry no key, so this is the common case until a producer stamps it."""
    import lance
    import pyarrow as pa

    from maintenance.core.lineage_emit import declared_table_id

    uri = str(tmp_path / "plain.lance")
    lance.write_dataset(pa.table({"v": [1]}), uri)

    assert declared_table_id(lance.dataset(uri)) is None


def test_the_PRODUCER_stamp_and_the_SWEEP_read_agree(tmp_path: Any) -> None:
    """The two halves of T6, asserted together — because either alone is silently useless.

    The medallion writer is the only party holding BOTH the URI (composed from the namespace) and the
    canonical table id (a separate project-qualified literal). `medallion/bronze` is both
    `bronze$events` and `bronze$pages`, so nothing downstream can derive one from the other. The writer
    declares; the sweep reads.

    Pinned as one test on purpose: a stamp under a key the reader does not look for, or a reader
    looking for a key nobody stamps, both leave the cascade's datasets emitting no provenance and — the
    part that matters — no per-dataset FAIL event, which is the estate's only maintenance failure
    surface for those tiers.
    """
    import lance
    import pyarrow as pa

    from maintenance.core.lineage_emit import declared_table_id
    from medallion.services.compute import LINEAGE_DATASET_ID_KEY, _with_declared_id

    stamped = _with_declared_id(pa.table({"v": [1, 2, 3]}), "silver$features")
    uri = str(tmp_path / "medallion-silver.lance")
    lance.write_dataset(stamped, uri)

    assert declared_table_id(lance.dataset(uri)) == "silver$features", "the sweep must read what the producer stamped"
    # The two modules must name the SAME key — they are in different services and cannot share a constant.
    from maintenance.core.lineage_emit import LINEAGE_DATASET_ID_KEY as READER_KEY

    assert LINEAGE_DATASET_ID_KEY == READER_KEY, "producer and reader disagree about the metadata key"


def test_the_stamp_PRESERVES_other_schema_metadata(tmp_path: Any) -> None:
    """A replace would destroy the #21 self-describing coordinates other producers write. Merge only."""
    import pyarrow as pa

    from medallion.services.compute import _with_declared_id

    table = pa.table({"v": [1]}).replace_schema_metadata({"lineage.run_id": "r-1", "owner": "data_eng"})
    out = _with_declared_id(table, "gold$catalog")

    metadata = {k.decode(): v.decode() for k, v in (out.schema.metadata or {}).items()}
    assert metadata["lineage.dataset_id"] == "gold$catalog"
    assert metadata["lineage.run_id"] == "r-1" and metadata["owner"] == "data_eng", "the stamp destroyed existing metadata"


def test_the_DECLARED_name_is_what_the_sweep_EMITS_under(tmp_path: Any) -> None:
    """The read half was DEAD CODE until this wiring: `declared_table_id` existed and nothing called it.

    `compact_one` holds the open dataset, so it reads the declaration there and carries it on the
    result; the emit path downstream has only a URI. That matters because for the cascade's own tiers a
    URI cannot be resolved to a name at all — `medallion/bronze` is both `bronze$events` and
    `bronze$pages` — so without the carry those datasets still emit nothing and T6 is unfinished while
    looking finished.

    Asserted end to end through `compact_one`, not on the helper, because the helper passing while the
    sweep ignores it is exactly the failure this closes.
    """
    from datetime import timedelta

    import lance
    import pyarrow as pa

    from maintenance.services.optimize import compact_one
    from medallion.services.compute import _with_declared_id

    uri = str(tmp_path / "medallion-gold.lance")
    lance.write_dataset(_with_declared_id(pa.table({"v": [1, 2, 3]}), "gold$catalog"), uri)

    result = compact_one(uri, {}, timedelta(days=7))

    assert result.error is None
    assert result.declared_table_id == "gold$catalog", "the sweep did not pick up the producer's declaration"


def test_an_UNDECLARED_dataset_still_falls_back_to_the_uri(tmp_path: Any) -> None:
    """Every dataset already on disk carries no key — the fallback must stay intact, not become None."""
    from datetime import timedelta

    import lance
    import pyarrow as pa

    from maintenance.core.lineage_emit import table_id_from_uri
    from maintenance.services.optimize import compact_one

    uri = str(tmp_path / "deadbeef_silver$plain.lance")
    lance.write_dataset(pa.table({"v": [1]}), uri)

    result = compact_one(uri, {}, timedelta(days=7))

    assert result.declared_table_id is None
    assert table_id_from_uri(result.uri) == "silver$plain.lance", "the URI derivation must still answer for undeclared datasets"


def test_the_maintenance_emitter_STAGES_the_event_rather_than_publishing_it_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    """The twin of the catalog's emit, and the second of the two `_PUBLISH_INTENT` pinned as bare.

    A sweep's lineage event describes a committed write, so losing one leaves the graph under-reporting
    work that really happened — the same loss mode as the cascade head, minus the halted run. With both
    routed, `_KNOWN_BARE_LINEAGE` is empty and #4's claim that "every lineage publish is staged" stops
    being aspirational.

    The invariants ratchet proves no BARE publish site REMAINS, which would hold just as well if the emit
    had been deleted. This asserts the other half: it still publishes, staged, keyed on the run id the
    relay will look for.
    """
    import asyncio as _asyncio

    from maintenance.core import lineage_emit as module

    calls: list[dict[str, object]] = []

    async def _fake_outbox(_publisher: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(module.outbox, "publish_lineage_with_outbox", _fake_outbox)
    emitter = DaprMaintenanceEmitter(
        cast(Any, object()),
        pubsub="maintenance-pubsub",
        topic="lineage.events.v1",
        job_namespace="compaction",
        timeout_seconds=5.0,
        outbox_uri="s3://staging/outbox",
        storage_options={"region": "eu-north-1"},
    )
    _asyncio.run(emitter._publish({"run": {"runId": "r-sweep"}}, "gold$catalog"))

    assert len(calls) == 1, "the maintenance emit did not reach the outbox"
    assert calls[0]["outbox_uri"] == "s3://staging/outbox"
    assert calls[0]["storage_options"] == {"region": "eu-north-1"}
    assert calls[0]["topic_name"] == "lineage.events.v1"
    assert calls[0]["run_id"] == "r-sweep"
