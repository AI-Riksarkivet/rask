"""Unit tests for catalog → lineage emission (P0 #3, ``catalog.core.lineage_emit``).

Infra-free: the pure event builder is checked directly, the HTTP emitter is exercised with a
fake client (best-effort: it must swallow failures), and a round-trip pins the wire contract
the lineage service ingests (``RunEvent`` parses the emitted event; the ``create_table``
operation string is shared by both sides).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from catalog.core import lineage_emit
from catalog.core.commit_ids import commit_run_id
from catalog.core.config import Settings
from catalog.core.lineage_emit import (
    CREATE_TABLE,
    DROP_TABLE,
    INSERT,
    MERGE_INSERT,
    BusEmitter,
    InputPin,
    InputRef,
    LineageEmitter,
    NoopEmitter,
    build_write_event,
    emit_write_event,
    make_emitter,
)
from catalog.core.run_context import RunContext
from lakehouse.events.bus import BusUnavailableError
from lakehouse.events.lineage_events import LANCE_FACET, DatasetUuids
from lakehouse.events.openlineage import DEFAULT_DATASET_NAMESPACE
from lakehouse.storage_options import StorageOptions
from lineage.models import RunEvent
from tests.bus_double import BusDouble
from tests.unit.no_store_session import no_store_session
from tests.unit.no_uuids import NO_OBJECT_UUIDS


#: The run a writer's pipeline names in its Context, a UUID as the spec's ``ParentRunFacet`` requires.
_PIPELINE_RUN = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a42"


def _parsed(event: dict):
    """The event through lineage's own models, whichever shape it is.

    A DDL change validates as `DatasetEvent` and a data write as `RunEvent`; a test that pins the
    round trip is pinning that the CONSUMER can read what the producer emits, not which class it got.
    """
    from lineage.models import DatasetEvent

    return DatasetEvent.model_validate(event) if "dataset" in event else RunEvent.model_validate(event)


def _written(event: dict) -> dict:
    """The dataset this event wrote, from whichever shape its operation produces.

    A DDL change is a `DatasetEvent` (no run, no job, one `dataset`); a data write stays a `RunEvent`
    with one output. The facet CONTENT is the same either way — that is the point of building both
    from one expression — so a test about a facet should not have to know which shape it got.
    """
    return event["dataset"] if "dataset" in event else event["outputs"][0]


def _facets(event: dict) -> dict:
    """Every facet describing the written dataset, run-slot and dataset-slot merged.

    On a DatasetEvent rask's `rask_author` and `rask_lance` facets ride the dataset, because there is no run to
    hang them on; on a RunEvent they ride the run. Merged here so one assertion covers both.
    """
    merged = dict(_written(event).get("facets") or {})
    merged.update((event.get("run") or {}).get("facets") or {})
    return merged


def test_build_write_event_stamps_the_project_so_watchers_can_be_found() -> None:
    """`rask_lance.project` is the tenant a notifier keys a project watch on (`spec/facets/rask/RaskLanceFacet.json`),
    so an event that names no project reaches its author and none of the project's watchers.

    Read where the published facet puts it: a create that commits no rows is a `DatasetEvent`, whose `rask_lance` facet
    rides the dataset because the event has no run to carry it."""
    event = build_write_event(
        table_id="acme-bronze$images",
        namespace="acme-bronze",
        author="alice",
        version=1,
        operation=CREATE_TABLE,
        run_id="r1",
        event_time="2026-06-24T00:00:00+00:00",
        job_namespace="lance-catalog",
        project="acme",
    )
    assert event["dataset"]["facets"][LANCE_FACET].get("project") == "acme"


def test_build_write_event_omits_an_unsafe_project_rather_than_qualifying_with_it() -> None:
    """A value outside the path-safe project shape must never become a lineage-name qualifier. Omitted, not
    sanitized: a project that cannot be named is one whose watchers must not be guessed at, and a project-less event
    reaches its author and no watchers, which is the safe direction."""
    event = build_write_event(
        table_id="alpha$bronze$images",
        namespace="alpha$bronze",
        author="alice",
        version=1,
        operation=CREATE_TABLE,
        run_id="r1",
        event_time="2026-06-24T00:00:00+00:00",
        job_namespace="lance-catalog",
        project="../etc/passwd",
    )
    assert "project" not in event["dataset"]["facets"][LANCE_FACET]


def test_build_write_event_attaches_schema_facet_and_round_trips() -> None:
    # #24 coverage fix: a catalog write carries the per-version column schema (blob/vector-aware) as the
    # standard SchemaDatasetFacet, so a graph backend the event is forwarded to shows real columns.
    fields = [{"name": "id", "type": "int64"}, {"name": "payload", "type": "blob"}]
    event = build_write_event(
        table_id="db$images",
        namespace="db",
        author="alice",
        version=1,
        operation=CREATE_TABLE,
        run_id="r1",
        event_time="2026-01-01T00:00:00+00:00",
        job_namespace="lance-catalog",
        schema_fields=fields,
    )
    schema = _written(event)["facets"]["schema"]
    assert schema["fields"] == fields
    assert schema["_producer"] and schema["_schemaURL"].endswith("SchemaDatasetFacet")


def test_build_write_event_without_schema_omits_facet() -> None:
    # No schema_fields (e.g. a reopen failed → []) must NOT plant an empty schema facet.
    event = build_write_event(
        table_id="db$t",
        namespace="db",
        author=None,
        version=1,
        operation=CREATE_TABLE,
        run_id="r1",
        event_time="t",
        job_namespace="lance-catalog",
    )
    assert "schema" not in _written(event).get("facets", {})


def test_emitted_event_round_trips_into_lineage_model() -> None:
    """The event the catalog emits must parse in the lineage service's RunEvent model, and its run carries no facet
    beyond rask's own and the engine's: a catalog write is triggered by its caller and has no schedule time, so it
    carries no ``nominalTime`` (ADR 0180)."""
    event = build_write_event(
        table_id="alpha$bronze$images",
        namespace="alpha$bronze",
        author="alice",
        version=1,
        operation=CREATE_TABLE,
        run_id="r1",
        event_time="2026-06-24T00:00:00+00:00",
        job_namespace="lance-catalog",
        commits_rows=True,
    )
    parsed = _parsed(event)
    assert parsed.operation == "create_table"
    assert parsed.author == "alice"
    assert parsed.outputs[0].name == "alpha$bronze$images"
    assert set(event["run"]["facets"]) == {"rask_author", "rask_lance", "processing_engine"}


def _emitting(bus: BusDouble) -> BusEmitter:
    """The catalog's emitter on ``bus``, which keeps what it publishes."""
    return BusEmitter(bus, "lineage.events.v1", job_namespace="lance-catalog", dataset_namespace=DEFAULT_DATASET_NAMESPACE, timeout_seconds=5.0)


def test_the_emitter_names_the_table_under_the_catalogs_endpoint_with_its_version_and_author() -> None:
    bus = BusDouble()
    asyncio.run(_emitting(bus).emit_create(table_id="a$b", namespace="a", author="alice", version=3))
    [sent] = bus.sent
    assert sent.topic == "lineage.events.v1"
    event = sent.event
    output = _written(event)
    # Every dataset is named under the catalog's endpoint, not the table's parent namespace ([[LH-553]]).
    assert output["namespace"] == DEFAULT_DATASET_NAMESPACE
    assert output["name"] == "a$b"
    assert output["facets"]["version"]["datasetVersion"] == "3"  # #20
    assert _facets(event)["rask_author"]["sub"] == "alice"


# --------------------------------------------------------------------------- #
# emit_write_event — the shared INLINE-await helper (durability: not BackgroundTasks)
# --------------------------------------------------------------------------- #


class _RecordingEmitter:
    """Records the emit_write call kwargs (stands in for the real emitter)."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def emit_write(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)


def test_emit_write_event_maps_segments_to_canonical_ids_and_awaits_inline() -> None:
    em = _RecordingEmitter()
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["db1", "users"],
            author="alice",
            version=3,
            operation="update",
            uuids=NO_OBJECT_UUIDS,
        )
    )
    # Awaited inline → exactly one recorded call (not deferred via BackgroundTasks, not dropped).
    assert len(em.writes) == 1
    w = em.writes[0]
    assert w["table_id"] == "db1$users"  # canonical delimited id == the OpenFGA object id == the catalog id
    assert w["namespace"] == "db1"  # parent namespace
    assert w["operation"] == "update"
    assert w["version"] == 3
    assert w["author"] == "alice"


def test_a_derived_write_event_pins_each_input_at_its_version_and_branch_under_the_writers_run() -> None:
    # A write derived from inputs names each at the version it consumed (the standard DatasetVersionDatasetFacet,
    # which lineage records on the edge), and an input read off a branch names that branch, because a branch
    # numbers its versions apart from main. The run the writer named is the event's standard `parent` run, and lineage
    # records it on every edge as the run the writer's own tooling answers to: the event's own run id is the catalog's,
    # derived from the commit.
    event = build_write_event(
        table_id="db$gold",
        namespace="db",
        author="alice",
        version=7,
        operation=MERGE_INSERT,
        run_id="r1",
        event_time="2026-07-21T00:00:00Z",
        job_namespace="catalog",
        inputs=[InputRef("db$silver", 4), InputRef("db$review", 2, "draft")],
        run=RunContext(run_id=_PIPELINE_RUN, job_namespace="pipelines", job="nightly"),
    )
    parent = {key: value for key, value in event["run"]["facets"]["parent"].items() if not key.startswith("_")}
    assert (parent, {"runId", "job"} & set(event["run"]["facets"]["rask_lance"])) == (
        {"run": {"runId": _PIPELINE_RUN}, "job": {"namespace": "pipelines", "name": "nightly"}},
        set(),
    )
    on_main, on_branch = event["inputs"]
    assert (on_main["facets"]["version"]["datasetVersion"], "rask_lance" in on_main["facets"]) == ("4", False)
    assert (on_branch["facets"]["version"]["datasetVersion"], on_branch["facets"]["rask_lance"]["ref"]) == ("2", "draft")
    from datetime import UTC, datetime

    from lineage.services.provenance import edges_of

    edges = edges_of(_parsed(event), received_at=datetime.now(UTC))
    assert sorted((e["in_table"], e["in_version"], e["in_branch"], e["out_table"], e["out_version"], e["writer_run_id"]) for e in edges) == [
        ("db$review", 2, "draft", "db$gold", 7, _PIPELINE_RUN),
        ("db$silver", 4, None, "db$gold", 7, _PIPELINE_RUN),
    ]


def test_emit_write_event_carries_the_pinned_source_version_through() -> None:
    # The #115 reproducibility seam: an InputPin resolves to the canonical id the graph keys on, with that SAME
    # version (found-live 2026-07-13: pins emitted then dropped → READ edges with no version).
    em = _RecordingEmitter()
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["db1", "dest"],
            author="alice",
            version=9,
            operation=MERGE_INSERT,
            inputs=[InputPin(segments=["db1", "src"], version=5)],
            uuids=NO_OBJECT_UUIDS,
        )
    )
    assert em.writes[0]["inputs"] == [InputRef("db1$src", 5)]  # the pin survives resolution


def test_emit_write_event_root_table_has_empty_namespace() -> None:
    # Boundary: a top-level (single-segment) table has no parent namespace → "".
    em = _RecordingEmitter()
    asyncio.run(emit_write_event(cast(LineageEmitter, em), ["t"], author=None, version=None, operation=INSERT, uuids=NO_OBJECT_UUIDS))
    assert em.writes[0]["namespace"] == ""
    assert em.writes[0]["version"] is None


def test_build_write_event_drop_is_versionless_and_named_drop_table() -> None:
    # #7a: a drop records a VERSIONLESS run named drop_table — the dataset node persists as history
    # (Marquez keeps dropped datasets too), so a reader can tell it was deleted, not just last-written.
    event = build_write_event(
        table_id="db$t",
        namespace="db",
        author="alice",
        version=None,
        operation=DROP_TABLE,
        run_id="r1",
        event_time="2026-01-01T00:00:00+00:00",
        job_namespace="lance-catalog",
    )
    assert "version" not in _written(event).get("facets", {})  # no version facet for a drop
    assert _facets(event)["rask_lance"]["operation"] == "drop_table"
    assert "version" not in _facets(event)["rask_lance"]
    assert "job" not in event, "a drop still mints a Job node for an operation nobody performed"


def test_an_emission_the_catalog_is_named_no_bus_for_is_refused_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emission switched on with no bus to publish on would run as the no-op, every write unannounced and nothing
    counted, so the settings are refused when they are read."""
    monkeypatch.setenv("LANCE_LINEAGE_EMIT_ENABLED", "true")
    monkeypatch.delenv("RASK_BUS_URL", raising=False)

    with pytest.raises(ValueError, match="RASK_BUS_URL"):
        Settings()


def test_the_catalog_emits_on_the_bus_or_not_at_all() -> None:
    def _make(**over: object) -> object:
        kw: dict[str, Any] = {
            "enabled": True,
            "bus": BusDouble(),
            "topic": "lineage.events.v1",
            "job_namespace": "lance-catalog",
            "dataset_namespace": DEFAULT_DATASET_NAMESPACE,
        }
        kw.update(over)
        return make_emitter(**kw)

    assert isinstance(_make(), BusEmitter)
    assert isinstance(_make(enabled=False), NoopEmitter)  # disabled: no-op
    assert isinstance(_make(bus=None), NoopEmitter)  # no connection to the bus: no-op


def _emit_failures(reader: InMemoryMetricReader) -> dict[str, int]:
    """`catalog.lineage_emit.failed` as ``reader`` exports it, by transport."""
    data = reader.get_metrics_data()
    return {
        str((point.attributes or {})["lance.catalog.transport"]): int(point.value)
        for resource in (data.resource_metrics if data is not None else ())
        for scope in resource.scope_metrics
        for metric in scope.metrics
        for point in metric.data.data_points
        if isinstance(point, NumberDataPoint)
    }


@pytest.mark.parametrize(
    ("outbox_at", "storage_options", "staged"),
    [
        pytest.param("lineage-outbox", None, True, id="the-outbox-holds-it"),
        pytest.param("occupied/lineage-outbox", None, False, id="the-stage-fails"),
        pytest.param("lineage-outbox", no_store_session, False, id="the-store-session-is-gone"),
    ],
)
def test_the_bus_emitter_swallows_a_failed_publish_and_says_whether_the_outbox_holds_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    outbox_at: str,
    storage_options: Callable[[], StorageOptions] | None,
    staged: bool,
) -> None:
    """A bus outage at publish never breaks the catalog write, and the line it logs says whether the relay
    can still deliver the event ([[LH-573]]). The failure is counted on a series the emitter created at 0 when it was
    built, so the first one is an increase the alert reads."""
    reader = InMemoryMetricReader()
    monkeypatch.setattr(
        lineage_emit, "_emit_failed", MeterProvider(metric_readers=[reader]).get_meter("lance.catalog").create_counter("catalog.lineage_emit.failed")
    )
    (tmp_path / "occupied").write_text("a file where the outbox's directory would be")
    emitter = BusEmitter(
        BusDouble(failing=BusUnavailableError("this process is not connected to the bus")),
        "lineage.events.v1",
        job_namespace="x",
        dataset_namespace=DEFAULT_DATASET_NAMESPACE,
        timeout_seconds=5.0,
        outbox_uri=f"file://{tmp_path}/{outbox_at}",
        storage_options=storage_options,
    )

    at_start = _emit_failures(reader)

    asyncio.run(emitter.emit_write(table_id="a$b", namespace="a", author=None, version=2, operation=INSERT))

    assert [getattr(record, "staged", None) for record in caplog.records if record.getMessage() == "lineage_publish_failed"] == [staged]
    assert (at_start, _emit_failures(reader)) == ({"derive": 0, "bus": 0}, {"derive": 0, "bus": 1})


# --- #19: lineage on every write (not just create) ---


def test_build_write_event_insert_omits_version_facet() -> None:
    event = build_write_event(
        table_id="a$b",
        namespace="a",
        author="alice",
        version=None,
        operation=INSERT,
        run_id="r1",
        event_time="t",
        job_namespace="lance-catalog",
    )
    assert event["job"]["name"] == "insert.a$b"
    assert _facets(event)["rask_lance"]["operation"] == "insert"
    assert "version" not in _facets(event)["rask_lance"]  # no version key on a version-less insert
    # The SUBJECT, stated directly. This was `"facets" not in …` while the version facet was the only
    # dataset facet a version-less insert could carry; `datasetType` is now always present ([[LIN-002]]),
    # so the blanket check would pass or fail for reasons unrelated to versions.
    assert "version" not in _written(event)["facets"]


def test_build_write_event_merge_carries_version() -> None:
    event = build_write_event(
        table_id="a$b",
        namespace="a",
        author=None,
        version=4,
        operation=MERGE_INSERT,
        run_id="r1",
        event_time="t",
        job_namespace="lance-catalog",
    )
    assert _facets(event)["rask_lance"]["operation"] == "merge_insert"
    assert _facets(event)["rask_lance"]["version"] == 4
    assert _written(event)["facets"]["version"]["datasetVersion"] == "4"
    assert "rask_author" not in _facets(event)


def test_a_write_is_announced_with_its_operation_and_version_under_the_commits_run_id() -> None:
    bus = BusDouble()
    emitter = _emitting(bus)

    def announce() -> str:
        uuids = DatasetUuids(table_uuid="0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01")
        asyncio.run(emitter.emit_write(table_id="a$b", namespace="a", author="alice", version=4, operation=MERGE_INSERT, uuids=uuids))
        return str(bus.events[-1]["run"]["runId"])

    first, second = announce(), announce()
    posted = bus.events[-1]
    assert posted["job"]["name"] == "merge_insert.a$b"
    assert posted["outputs"][0]["facets"]["version"]["datasetVersion"] == "4"
    # Two announcements of one commit are one run ([[LH-497]]), under the empty key of a stack with no store.
    assert first == second == commit_run_id(b"", table_uuid="0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01", branch=None, branch_identifier=None, version=4)


@pytest.mark.asyncio
async def test_a_SERVICE_run_can_name_the_person_it_runs_FOR() -> None:
    """ORIGINATOR, for the catalog's own emits.

    Some catalog writes are made BY a service ON BEHALF OF a person, under the service's own account.
    `enforce_author` overwrites `author` with that service's verified principal, so the human CANNOT be the
    author and must not try to be: `rask_lance.originator` is the field that names them.

    It is a TARGETING hint and authorizes nothing: a notifier re-derives every recipient's visibility at
    delivery, so a forged one can at worst tell a person about a run whose outputs they can already see.
    """
    bus = BusDouble()

    await _emitting(bus).emit_create(
        table_id="acme-silver$annotations", namespace="acme-silver", author="service-publisher", version=1, originator="dex~08a8684b-db88"
    )

    [event] = bus.events
    lance = _facets(event)["rask_lance"]
    assert lance["originator"] == "dex~08a8684b-db88"
    assert _facets(event)["rask_author"]["sub"] == "service-publisher", "the service remains the author; originator is additive"


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", ["system"])
async def test_a_NON_PERSONAL_originator_is_DROPPED(junk: str) -> None:
    """An originator is an address, so it must identify a person; a role, a wildcard or a userset is not one, and
    carrying it would address whatever it names (`lakehouse.governed.subjects.is_person_subject`)."""
    bus = BusDouble()

    await _emitting(bus).emit_create(table_id="db$t", namespace="db", author="svc", version=1, originator=junk)

    [event] = bus.events
    assert "originator" not in _facets(event)["rask_lance"]
