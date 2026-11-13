"""Unit tests for compaction → lineage emission (#7b + §4 failure visibility) — infra-free.

Drives the async emit with stdlib ``asyncio.run`` (the project convention). Covers:
* the OpenLineage maintenance-event shapes (COMPLETE and FAIL) **round-tripped through the lineage
  ``RunEvent`` model** — the cross-service wire contract,
* which datasets in a sweep get which event (material work → COMPLETE; ``maintain:``-errored → FAIL;
  ``open:``-errored / no-op / unnamed → nothing) and the location each names its table at,
* the FAIL flood guard (deterministic per-dataset run id; COMPLETE keeps uuid4), the per-tick FAIL cap
  (logged, never silent) and the concurrent (bounded) FAIL fan-out,
* the emitter factory (no-op when off/unwired; Dapr when wired) and the best-effort publish (a broker
  outage must never fail a sweep).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from lakehouse.lineage_events import DatasetUuids
from lakehouse.storage_options import StorageOptions
from lineage.models import RunEvent
from maintenance.core.lineage_emit import (
    EXPIRE_VERSIONS,
    UNRESOLVED,
    DaprMaintenanceEmitter,
    NoopEmitter,
    build_maintenance_event,
    build_maintenance_fail_event,
    classify_retryable,
    make_emitter,
)
from maintenance.services.expiry import DatasetResult
from maintenance.services.table_pass import emit_pass_lineage
from tests.unit.no_store_session import no_store_session


# --------------------------------------------------------------------------- #
# maintenance event shape + the cross-service wire contract
# --------------------------------------------------------------------------- #


def test_maintenance_event_round_trips_through_lineage_run_event() -> None:
    # The wire contract: the lineage service must parse a compaction event as a successful, input-less run on
    # the dataset — so it records the run and asserts no edge (nothing was derived from anything). Its run carries no
    # `nominalTime`: a sweep tick is no schedule time of the table's run (ADR 0180).
    event = build_maintenance_event(
        table_id="silver$features",
        namespace="silver",
        job_namespace="compaction",
        run_id="r2",
        event_time="2026-06-30T00:00:00Z",
    )
    parsed = RunEvent.model_validate(event)
    assert parsed.is_success
    assert parsed.operation == EXPIRE_VERSIONS
    assert [d.name for d in parsed.outputs] == ["silver$features"]
    assert parsed.inputs == []
    assert set(event["run"]["facets"]) == {"rask_lance"}


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


def test_fail_event_round_trips_through_lineage_run_event() -> None:
    # The wire contract: lineage parses it as a FAILED run on the dataset, which the feed records and which
    # asserts no edge, because a failed run derived nothing.
    event = _fail_event()
    parsed = RunEvent.model_validate(event)
    assert set(event["run"]["facets"]) == {"errorMessage", "rask_lance"}
    assert not parsed.is_success
    assert parsed.operation == EXPIRE_VERSIONS
    assert [d.name for d in parsed.outputs] == ["ns$table"]
    assert parsed.run.facets["errorMessage"]["message"] == "maintain: Commit conflict for version 42"


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
    """Captures (table_id, location) per COMPLETE emit and (table_id, location, error) per FAIL emit."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.failed_calls: list[tuple[str, str | None, str]] = []

    async def emit_maintenance(self, *, table_id: str, location: str | None = None, uuids: DatasetUuids = UNRESOLVED) -> None:
        self.calls.append((table_id, location))

    async def emit_maintenance_failed(self, *, table_id: str, error: str, location: str | None = None, uuids: DatasetUuids = UNRESOLVED) -> None:
        self.failed_calls.append((table_id, location, error))


def _result(uri: str, **kw: Any) -> DatasetResult:
    """A dataset no ``__manifest`` row named, as the sweep reports one: no table id."""
    return DatasetResult(uri=uri, **kw)


def _named(table: str, **kw: Any) -> DatasetResult:
    """A dataset the sweep named ``table`` through ``__manifest`` ([[LH-520]])."""
    return DatasetResult(uri=f"s3://b/{table}", table_id=table, **kw)


def test_emit_pass_lineage_emits_only_for_datasets_whose_versions_were_reclaimed() -> None:
    emitter = _RecordingEmitter()
    results = [
        _named("ns$a", old_versions_removed=3),  # compacted → emit
        _named("ns$b", old_versions_removed=2),  # GC'd → emit
        _named("ns$c"),  # no-op tick → skip
        _named("ns$d", old_versions_removed=1, error="open: boom"),  # transient noise → skip
        _result("s3://b/nounderscore", old_versions_removed=1),  # no __manifest row names it → skip
    ]
    asyncio.run(emit_pass_lineage(cast(Any, emitter), results))
    assert emitter.calls == [("ns$a", "s3://b/ns$a"), ("ns$b", "s3://b/ns$b")]
    assert emitter.failed_calls == []  # an open:-errored dir NEVER produces an event (§4 selection)


def test_emit_pass_lineage_fails_only_for_maintain_errors() -> None:
    # §4 selection: maintain: (escaped compact/GC — post-auto-retry terminal) → FAIL; open: (unreadable /
    # declared-only dir) → nothing; a maintain:-errored dataset that ALSO did partial material work gets
    # the FAIL, not a COMPLETE (the pass did not complete); a dataset no __manifest row names → nothing either way.
    emitter = _RecordingEmitter()
    results = [
        _named("ns$a", error="maintain: Commit conflict"),
        _named("ns$b", old_versions_removed=2, error="maintain: cleanup blew up"),  # partial work
        _named("ns$c", error="open: not a dataset"),
        _named("ns$d", error="boom"),  # UNPREFIXED error → neither event (selection contract)
        _result("s3://b/medallion", error="maintain: nope"),  # no __manifest row names it → skip
        _named("ns$e", old_versions_removed=1),  # healthy → COMPLETE as before
    ]
    asyncio.run(emit_pass_lineage(cast(Any, emitter), results))
    assert emitter.calls == [("ns$e", "s3://b/ns$e")]
    assert emitter.failed_calls == [
        ("ns$a", "s3://b/ns$a", "maintain: Commit conflict"),
        ("ns$b", "s3://b/ns$b", "maintain: cleanup blew up"),
    ]


def test_emit_pass_lineage_never_raises_even_for_a_broken_emitter() -> None:
    # The guardrail is structural: "a publish failure must never fail the sweep" holds even for an
    # emitter that predates the FAIL protocol (AttributeError while building the coros) or one whose
    # coroutine raises — either way the cron handler must never see it.
    class _OldProtocolEmitter:
        async def emit_maintenance(self, *, table_id: str, location: str | None = None) -> None:
            return None

    class _RaisingEmitter(_RecordingEmitter):
        async def emit_maintenance_failed(self, *, table_id: str, error: str, location: str | None = None, uuids: DatasetUuids = UNRESOLVED) -> None:
            raise RuntimeError("boom from a non-best-effort emitter")

    results = [_named("ns$a", error="maintain: x")]
    asyncio.run(emit_pass_lineage(cast(Any, _OldProtocolEmitter()), results))  # no raise
    asyncio.run(emit_pass_lineage(cast(Any, _RaisingEmitter()), results))  # no raise


# --------------------------------------------------------------------------- #
# emitter factory + transport behavior
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("enabled", "dapr", "emitter_type"),
    [
        pytest.param(False, None, NoopEmitter, id="disabled"),
    ],
)
def test_make_emitter_is_noop_when_disabled(enabled: bool, dapr: Any, emitter_type: type) -> None:
    emitter = make_emitter(
        enabled=enabled,
        dapr=dapr,
        pubsub="lineage-pubsub",
        topic="lineage.events.v1",
        job_namespace="compaction",
        dataset_namespace="lance-namespace://localhost/api/catalog",
    )
    assert isinstance(emitter, emitter_type)


class _FakeDaprClient:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"pubsub": pubsub_name, "topic": topic_name, "event": json.loads(data)["data"]})


def test_dapr_emitter_publishes_to_configured_pubsub_and_topic() -> None:
    client = _FakeDaprClient()
    emitter = DaprMaintenanceEmitter(
        cast(Any, client),
        pubsub="lineage-pubsub",
        topic="lineage.events.v1",
        job_namespace="compaction",
        dataset_namespace="lance-namespace://localhost/api/catalog",
        timeout_seconds=5.0,
    )
    asyncio.run(emitter.emit_maintenance(table_id="ns$a"))
    assert len(client.published) == 1
    assert client.published[0]["pubsub"] == "lineage-pubsub"
    assert client.published[0]["topic"] == "lineage.events.v1"
    payload = client.published[0]["event"]
    assert payload["outputs"][0]["name"] == "ns$a"
    assert payload["run"]["facets"]["rask_lance"]["operation"] == EXPIRE_VERSIONS


@pytest.mark.parametrize(
    ("outbox_at", "storage_options", "staged"),
    [
        pytest.param("lineage-outbox", None, True, id="the-outbox-holds-it"),
        pytest.param("occupied/lineage-outbox", None, False, id="the-stage-fails"),
        pytest.param("lineage-outbox", no_store_session, False, id="the-store-session-is-gone"),
    ],
)
def test_a_dapr_emitter_swallows_a_failed_publish_and_says_whether_the_outbox_holds_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, outbox_at: str, storage_options: Callable[[], StorageOptions] | None, staged: bool
) -> None:
    """A sidecar or broker outage never fails a maintenance sweep, and the line it logs says whether the relay can still
    deliver the event ([[LH-573]])."""

    class _BoomClient:
        """The sidecar's publish, with `dapr.aio.clients.DaprClient.publish_event`'s signature: it fails."""

        async def publish_event(
            self,
            pubsub_name: str,
            topic_name: str,
            data: bytes | str,
            publish_metadata: dict[str, str] | None = None,
            metadata: tuple[tuple[str, str | bytes], ...] | None = None,
            data_content_type: str | None = None,
        ) -> None:
            raise RuntimeError("sidecar down")

    (tmp_path / "occupied").write_text("a file where the outbox's directory would be")
    emitter = DaprMaintenanceEmitter(
        cast(Any, _BoomClient()),
        pubsub="p",
        topic="t",
        job_namespace="compaction",
        timeout_seconds=5.0,
        dataset_namespace="lance-namespace://localhost/api/catalog",
        outbox_uri=f"file://{tmp_path}/{outbox_at}",
        storage_options=storage_options,
    )

    asyncio.run(emitter.emit_maintenance(table_id="ns$a"))

    assert [getattr(record, "staged", None) for record in caplog.records if record.getMessage() == "maintenance_publish_failed"] == [staged]


def test_fail_run_id_is_deterministic_per_dataset_and_complete_stays_random() -> None:
    # The flood guard (§4): every tick's FAIL for the same dataset carries the SAME run id, so the graph
    # MERGEs one (:Run) node and /events dedups the redelivered terminal — while COMPLETE keeps uuid4
    # (each materially-compacting tick IS a distinct run; §4 says do NOT change that).
    client = _FakeDaprClient()
    emitter = DaprMaintenanceEmitter(
        cast(Any, client), pubsub="p", topic="t", job_namespace="compaction", timeout_seconds=5.0, dataset_namespace="lance-namespace://localhost/api/catalog"
    )

    async def drive() -> None:
        await emitter.emit_maintenance_failed(table_id="ns$a", error="maintain: t1")
        await emitter.emit_maintenance_failed(table_id="ns$a", error="maintain: t2")
        await emitter.emit_maintenance_failed(table_id="ns$b", error="maintain: t1")
        await emitter.emit_maintenance(table_id="ns$a")
        await emitter.emit_maintenance(table_id="ns$a")

    asyncio.run(drive())
    run_ids = [p["event"]["run"]["runId"] for p in client.published]
    fail_a1, fail_a2, fail_b, complete_1, complete_2 = run_ids
    assert fail_a1 == fail_a2  # two ticks, one dataset → ONE Run node
    assert fail_b != fail_a1  # per-dataset, not global
    assert complete_1 != complete_2  # COMPLETE semantics untouched (uuid4 per tick)
    assert complete_1 not in {fail_a1, fail_b}


def test_the_stamp_PRESERVES_other_schema_metadata(tmp_path: Any) -> None:
    """A replace would destroy the #21 self-describing coordinates other producers write. Merge only."""
    import pyarrow as pa

    from lakehouse.stage_stamp import declare_dataset_id

    table = pa.table({"v": [1]}).replace_schema_metadata({"lineage.run_id": "r-1", "owner": "data_eng"})
    out = declare_dataset_id(table, "gold$catalog")

    metadata = {k.decode(): v.decode() for k, v in (out.schema.metadata or {}).items()}
    assert metadata["lineage.dataset_id"] == "gold$catalog"
    assert metadata["lineage.run_id"] == "r-1" and metadata["owner"] == "data_eng", "the stamp destroyed existing metadata"


def test_a_pass_is_named_only_by_the_table_the_sweep_named(tmp_path: Any) -> None:
    """[[LH-208]] [[LH-520]]: a maintenance pass names the table ``__manifest`` named its dataset, and a stamp names nothing.

    `lineage.dataset_id` is schema metadata, so a writer could re-point it at another table, and a pass named by it
    would file its RunEvents and audit records against that table. A dataset the sweep named nothing for emits nothing,
    whatever its stamp says.

    Driven through expiry on real pylance into the sweep's own emit, because the name is carried by one and spent in
    the other.
    """
    import lance
    import pyarrow as pa

    from lakehouse.stage_stamp import declare_dataset_id
    from maintenance.services.expiry import ExpiryPolicy, ExpiryTarget, expire_versions

    forged = "victim$payroll"
    named, unnamed = str(tmp_path / "0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f"), str(tmp_path / "medallion" / "gold")
    for uri in (named, unnamed):
        lance.write_dataset(declare_dataset_id(pa.table({"v": [1]}), forged), uri, mode="create")
        lance.write_dataset(pa.table({"v": [2]}), uri, mode="append")  # a second version, so the pass reclaims one
    policy = ExpiryPolicy(older_than=None, retain_versions=1)

    results = [
        expire_versions(ExpiryTarget(uri=named, storage_options={}, write_options={}, table_id="silver$plain"), policy),
        expire_versions(ExpiryTarget(uri=unnamed, storage_options={}, write_options={}), policy),
    ]
    assert [(r.error, r.old_versions_removed) for r in results] == [(None, 1), (None, 1)], results

    emitter = _RecordingEmitter()
    asyncio.run(emit_pass_lineage(cast(Any, emitter), results))
    assert emitter.calls == [("silver$plain", named)], "a pass was named by a stamp"
