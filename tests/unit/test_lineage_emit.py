"""Unit tests for catalog → lineage emission (P0 #3, ``catalog.core.lineage_emit``).

Infra-free: the pure event builder is checked directly, the HTTP emitter is exercised with a
fake client (best-effort: it must swallow failures), and a round-trip pins the wire contract
the lineage service ingests (``RunEvent`` parses the emitted event; the ``create_table``
operation string is shared by both sides).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx
import pytest

from catalog.core.commit_ids import commit_run_id
from catalog.core.lineage_emit import (
    CREATE_TABLE,
    DROP_TABLE,
    INSERT,
    MERGE_INSERT,
    PUBLISH_TABLE,
    DaprEmitter,
    HttpLineageEmitter,
    InputPin,
    InputRef,
    LineageEmitter,
    NoopEmitter,
    build_write_event,
    emit_write_event,
    make_emitter,
)
from lineage.models import RunEvent
from service_kit.openlineage import DatasetUuids
from tests.unit.no_uuids import NO_OBJECT_UUIDS


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
    """THE WATCH DEFECT. `lance_fields` carried `operation` + `version` and nothing else, so every
    catalog-authored event reached ZERO watchers estate-wide.

    `notifications/api/lineage_events.py::project_id` reads `run.facets.rask_lance.project`, and
    `fanout.py:88` skips the watcher loop ENTIRELY when it is `None`. So a table created, dropped,
    renamed or reindexed in a project nobody could hear about — not because the watch was wrong, but
    because the producer never named the tenant. `ingest/lineage.py:213-215` had it right all along.

    Asserted through `project_id` rather than only on the dict key: the key name IS the contract with
    a consumer in another deployable, and a test on `lance["project"]` alone would still pass if the
    reader looked somewhere else."""
    from notifications.api.lineage_events import LineageRunEvent, project_id

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
    assert _facets(event)["rask_lance"]["project"] == "acme"
    # Validated into the CONSUMER's model (notifications' own `LineageRunEvent`), not lineage's
    # `RunEvent`: the assertion worth making is that the plane which reads this wire JSON finds the
    # tenant, and only its parser proves that.
    assert project_id(LineageRunEvent.model_validate(event).run) == "acme"


def test_build_write_event_omits_an_unsafe_project_rather_than_qualifying_with_it() -> None:
    """The same guard `ingest.naming.tenant` applies, for the reason stated there: a value outside the
    path-safe shape must never become a lineage-name qualifier. Omitted, not sanitized — a project that
    cannot be named is one whose watchers must not be guessed at, and silence is the safe direction
    (`project_id`'s own docstring: a project-less run reaches its author and no watchers)."""
    from notifications.api.lineage_events import LineageRunEvent, project_id

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
    assert "project" not in _facets(event)["rask_lance"]
    assert project_id(LineageRunEvent.model_validate(event).run) is None


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


class _Resp:
    def raise_for_status(self) -> None:
        return None


class _CapturingClient:
    """Fake httpx client capturing the posted JSON + headers."""

    def __init__(self) -> None:
        self.posted: Any = None
        self.headers: Any = None

    async def post(self, *_args: object, **kwargs: object) -> _Resp:
        self.posted = kwargs["json"]
        self.headers = kwargs.get("headers")
        return _Resp()


class _BoomClient:
    async def post(self, *_args: object, **_kwargs: object) -> _Resp:
        raise httpx.ConnectError("lineage down")


def test_http_emitter_posts_the_event() -> None:
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage/api/v1/lineage", job_namespace="lance-catalog")
    asyncio.run(emitter.emit_create(table_id="a$b", namespace="a", author="alice", version=3))
    assert client.posted is not None
    output = _written(client.posted)
    assert output["namespace"] == "a"
    assert output["name"] == "a$b"
    assert output["facets"]["version"]["datasetVersion"] == "3"  # #20
    assert _facets(client.posted)["rask_author"]["sub"] == "alice"


def test_http_emitter_forwards_authorization() -> None:
    # So ingest accepts the event when the lineage service has OIDC on (else 401 + silent drop).
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage", job_namespace="lance-catalog")
    asyncio.run(emitter.emit_create(table_id="a$b", namespace="a", author="alice", version=1, authorization="Bearer xyz"))
    assert client.headers == {"Authorization": "Bearer xyz"}


# --------------------------------------------------------------------------- #
# emit_write_event — the shared INLINE-await helper (durability: not BackgroundTasks)
# --------------------------------------------------------------------------- #


class _RecordingEmitter:
    """Records the emit_write call kwargs (stands in for the real emitter)."""

    def __init__(self, project: str | None = None) -> None:
        self.writes: list[dict[str, Any]] = []
        self.resolved: list[str] = []
        self._project = project

    async def project_for(self, top_ns: str) -> str | None:
        """Part of the `LineageEmitter` protocol, so the fake carries it too. Recording the argument
        is the point of the assertion below: the tenant must be resolved from the table's TOP
        namespace segment, which is the rung the warehouse registry actually binds."""
        self.resolved.append(top_ns)
        return self._project

    async def emit_write(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)


def test_emit_write_event_resolves_the_tenant_from_the_top_namespace_segment() -> None:
    """The eight call sites have no project in scope, so `emit_write_event` resolves it once and
    forwards it. Pinned because getting the SEGMENT wrong is silent: resolving `bronze` instead of
    `alpha` finds no binding, returns None, and the write goes out watcher-less exactly as before."""
    em = _RecordingEmitter(project="acme")
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["alpha", "bronze", "images"],
            author="alice",
            version=1,
            operation=INSERT,
            authorization=None,
            uuids=NO_OBJECT_UUIDS,
        )
    )
    assert em.resolved == ["alpha"]
    assert em.writes[0]["project"] == "acme"


def test_emit_write_event_maps_segments_to_canonical_ids_and_awaits_inline() -> None:
    em = _RecordingEmitter()
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["db1", "users"],
            author="alice",
            version=3,
            operation="update",
            authorization="Bearer x",
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
    assert w["authorization"] == "Bearer x"


def test_build_write_event_records_derived_from_inputs() -> None:
    # A rename must record its SOURCE as an input (DERIVED_FROM) so the destination is not an orphan.
    event = build_write_event(
        table_id="db1$renamed",
        namespace="db1",
        author="alice",
        version=None,
        operation="register_table",
        run_id="r1",
        event_time="2026-07-15T00:00:00Z",
        job_namespace="catalog",
        inputs=[InputRef("db1", "db1$orig", None)],
    )
    # An unversioned input edge carries no version facet — just the (namespace, name) source node.
    assert event["inputs"] == [{"namespace": "db1", "name": "db1$orig"}]
    assert _written(event)["name"] == "db1$renamed"


def test_a_derived_write_event_pins_each_input_at_its_version_and_branch() -> None:
    # A write derived from inputs names each at the version it consumed (the standard DatasetVersionDatasetFacet,
    # which lineage records on the edge), and an input read off a branch names that branch, because a branch
    # numbers its versions apart from main.
    event = build_write_event(
        table_id="db$gold",
        namespace="db",
        author="alice",
        version=7,
        operation=MERGE_INSERT,
        run_id="r1",
        event_time="2026-07-21T00:00:00Z",
        job_namespace="catalog",
        inputs=[InputRef("db", "db$silver", 4), InputRef("db", "db$review", 2, "draft")],
    )
    on_main, on_branch = event["inputs"]
    assert (on_main["facets"]["version"]["datasetVersion"], "rask_lance" in on_main["facets"]) == ("4", False)
    assert (on_branch["facets"]["version"]["datasetVersion"], on_branch["facets"]["rask_lance"]["ref"]) == ("2", "draft")
    from datetime import UTC, datetime

    from lineage.services.provenance import edges_of

    edges = edges_of(_parsed(event), received_at=datetime.now(UTC))
    assert sorted((e["in_table"], e["in_version"], e["in_branch"], e["out_table"], e["out_version"]) for e in edges) == [
        ("db$review", 2, "draft", "db$gold", 7),
        ("db$silver", 4, None, "db$gold", 7),
    ]


def test_emit_write_event_maps_input_pins_to_canonical_dataset_ids() -> None:
    # The rename handler passes the SOURCE segments as an InputPin; emit_write_event must resolve them to the
    # SAME canonical (namespace, id) the rest of the graph keys on, so the DERIVED_FROM edge points at the
    # real source node. An unpinned source carries version=None (no reproducibility facet).
    em = _RecordingEmitter()
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["db1", "dest"],
            author="alice",
            version=None,
            operation="register_table",
            authorization=None,
            inputs=[InputPin(segments=["db1", "src"])],
            uuids=NO_OBJECT_UUIDS,
        )
    )
    # (parent namespace, canonical id, pinned version) of the source, as a resolved InputRef.
    assert em.writes[0]["inputs"] == [InputRef("db1", "db1$src", None)]


def test_emit_write_event_carries_the_pinned_source_version_through() -> None:
    # The #115 reproducibility seam: an InputPin with a concrete version must resolve to an InputRef carrying
    # that SAME version (found-live 2026-07-13: pins emitted then dropped → READ edges with no version). This
    # is the version!=None twin of the test above, so a refactor that drops pin.version reddens here.
    em = _RecordingEmitter()
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["db1", "dest"],
            author="alice",
            version=9,
            operation=MERGE_INSERT,
            authorization=None,
            inputs=[InputPin(segments=["db1", "src"], version=5)],
            uuids=NO_OBJECT_UUIDS,
        )
    )
    assert em.writes[0]["inputs"] == [InputRef("db1", "db1$src", 5)]  # the pin survives resolution


def test_emit_write_event_root_table_has_empty_namespace() -> None:
    # Boundary: a top-level (single-segment) table has no parent namespace → "".
    em = _RecordingEmitter()
    asyncio.run(emit_write_event(cast(LineageEmitter, em), ["t"], author=None, version=None, operation=INSERT, authorization=None, uuids=NO_OBJECT_UUIDS))
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


def test_http_emitter_swallows_failures() -> None:
    # Best-effort: a down/erroring lineage service must NOT propagate out of a catalog write.
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, _BoomClient()), "http://lineage", job_namespace="lance-catalog")
    asyncio.run(emitter.emit_create(table_id="a$b", namespace="a", author="alice", version=1))  # no raise


def test_make_emitter_selects_implementation() -> None:
    client = cast(httpx.AsyncClient, object())

    def _make(**over: object) -> object:
        kw: dict[str, object] = {
            "enabled": True,
            "transport": "http",
            "url": "http://lineage",
            "client": client,
            "dapr": None,
            "pubsub": "lineage-pubsub",
            "topic": "lineage.events.v1",
            "job_namespace": "lance-catalog",
        }
        kw.update(over)
        return make_emitter(**kw)  # ty: ignore[invalid-argument-type]  # dict[str, object] kwargs fan-out

    assert isinstance(_make(), HttpLineageEmitter)
    assert isinstance(_make(enabled=False), NoopEmitter)  # disabled → no-op
    assert isinstance(_make(url=None), NoopEmitter)  # http transport unwired → no-op
    assert isinstance(_make(transport="dapr", dapr=object()), DaprEmitter)  # durable Dapr transport
    assert isinstance(_make(transport="dapr", dapr=None), NoopEmitter)  # dapr unwired → no-op


class _FakeDapr:
    """Just enough of the async DaprClient to capture a publish_event (or fail one)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self._fail = fail

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        if self._fail:
            raise RuntimeError("sidecar down")
        self.published.append(
            {
                "pubsub": pubsub_name,
                "topic": topic_name,
                "content_type": data_content_type,
                "event": json.loads(data),
            }
        )


def test_dapr_emitter_publishes_event_to_topic() -> None:
    dapr = _FakeDapr()
    emitter = DaprEmitter(
        cast(Any, dapr),
        "lineage-pubsub",
        "lineage.events.v1",
        job_namespace="lance-catalog",
        timeout_seconds=5.0,
    )
    asyncio.run(emitter.emit_create(table_id="a$b", namespace="a", author="alice", version=1))
    assert len(dapr.published) == 1
    pub = dapr.published[0]
    assert pub["pubsub"] == "lineage-pubsub" and pub["topic"] == "lineage.events.v1"
    assert pub["content_type"] == "application/json"
    assert _written(pub["event"])["name"] == "a$b"  # the spec-correct OpenLineage event is the data
    assert _facets(pub["event"])["rask_author"]["sub"] == "alice"  # the verified catalog author


def test_dapr_emitter_swallows_publish_failure() -> None:
    # A sidecar/broker outage at publish must never break the catalog write (best-effort, like HTTP).
    emitter = DaprEmitter(
        cast(Any, _FakeDapr(fail=True)),
        "lineage-pubsub",
        "lineage.events.v1",
        job_namespace="x",
        timeout_seconds=5.0,
    )
    asyncio.run(emitter.emit_write(table_id="a$b", namespace="a", author=None, version=2, operation=INSERT))  # no raise


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


def test_http_emitter_emit_write_posts_operation_and_version_under_the_commits_run_id() -> None:
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage/api/v1/lineage", job_namespace="lance-catalog")

    def announce() -> str:
        uuids = DatasetUuids(table_uuid="0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01")
        asyncio.run(emitter.emit_write(table_id="a$b", namespace="a", author="alice", version=4, operation=MERGE_INSERT, uuids=uuids))
        assert client.posted is not None
        return str(client.posted["run"]["runId"])

    first, second = announce(), announce()
    assert client.posted is not None
    assert client.posted["job"]["name"] == "merge_insert.a$b"
    assert client.posted["outputs"][0]["facets"]["version"]["datasetVersion"] == "4"
    # Two announcements of one commit are one run ([[LH-497]]), under the empty key of a stack with no store.
    assert first == second == commit_run_id(b"", table_uuid="0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01", branch=None, branch_identifier=None, version=4)
    # A tag move names the version another write committed, and is never that commit's run.
    uuids = DatasetUuids(table_uuid="0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01")
    asyncio.run(emitter.emit_write(table_id="a$b", namespace="a", author="alice", version=4, operation=PUBLISH_TABLE, uuids=uuids))
    assert client.posted["run"]["runId"] != first


@pytest.mark.asyncio
async def test_a_catalog_write_RESOLVES_its_tenant_so_watchers_are_reachable() -> None:
    """Trap 3, closed at the one place that can close it.

    `rask_lance.project` is what the notifications plane's WATCH fan-out keys on: `fanout.py` skips the
    watcher loop ENTIRELY when it is None. The catalog had the whole mechanism — a `project` kwarg on
    every emit, an `is_safe_project` guard, and a registry-backed `project_for` resolver — and not one
    caller passed it, so no catalog write reached a watcher anywhere in the estate.

    Resolving it HERE rather than at each of the call sites is deliberate: the mapping is mechanical
    (top namespace → registry binding → tenant), and a per-caller kwarg is a rule every future
    endpoint has to remember. This is the "one place" version.
    """
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage", job_namespace="lance-catalog")

    async def _resolver(top_ns: str) -> str | None:
        return "acme" if top_ns == "acme-bronze" else None

    emitter._project_resolver = _resolver  # noqa: SLF001 — `make_emitter` wires this in production

    await emitter.emit_create(table_id="acme-bronze$events", namespace="acme-bronze", author="alice", version=1)

    lance = _facets(client.posted)["rask_lance"]
    assert lance["project"] == "acme", "without this the watcher loop is skipped and every watcher is silently lost"


@pytest.mark.asyncio
async def test_an_EXPLICIT_project_is_not_overridden_by_resolution() -> None:
    """A caller that knows the tenant beats a lookup — resolution fills a gap, it does not decide."""
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage", job_namespace="lance-catalog")

    async def _resolver(_top_ns: str) -> str | None:
        return "wrong-tenant"

    emitter._project_resolver = _resolver  # noqa: SLF001

    await emitter.emit_create(table_id="acme-bronze$events", namespace="acme-bronze", author="alice", version=1, project="acme")

    assert _facets(client.posted)["rask_lance"]["project"] == "acme"


@pytest.mark.asyncio
async def test_a_SERVICE_run_can_name_the_person_it_runs_FOR() -> None:
    """Q2 — ORIGINATOR — for the catalog's own emits.

    Some catalog writes are made BY a service ON BEHALF OF a person: the annotator's publish saga is
    the live case, driven from an actor reminder under a dedicated `publisher@rask.internal` service
    account. `enforce_author` correctly overwrites `author` with that service's verified principal, so the
    human CANNOT be the author and must not try to be — `rask_lance.originator` is the field for exactly
    this, and the catalog had no way to carry it.

    It is a TARGETING hint and authorizes nothing: the notifications plane re-derives every
    recipient's visibility at delivery, so a forged one can at worst put a row in an inbox whose
    owner can already see the run's outputs.
    """
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage", job_namespace="lance-catalog")

    await emitter.emit_create(
        table_id="acme-silver$annotations", namespace="acme-silver", author="service-publisher", version=1, originator="dex~08a8684b-db88"
    )

    lance = _facets(client.posted)["rask_lance"]
    assert lance["originator"] == "dex~08a8684b-db88"
    assert _facets(client.posted)["rask_author"]["sub"] == "service-publisher", "the service remains the author; originator is additive"


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", ["system"])
async def test_a_NON_PERSONAL_originator_is_DROPPED(junk: str) -> None:
    """Trap 4. An address must identify a person; a role, a wildcard or a userset is not one, and
    carrying it writes into an inbox actor literally named that."""
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage", job_namespace="lance-catalog")

    await emitter.emit_create(table_id="db$t", namespace="db", author="svc", version=1, originator=junk)

    assert "originator" not in _facets(client.posted)["rask_lance"]
