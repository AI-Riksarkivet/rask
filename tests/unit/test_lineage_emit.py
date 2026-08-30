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

from catalog.core.lineage_emit import (
    CREATE_TABLE,
    DECLARE_TABLE,
    DEREGISTER_TABLE,
    DROP_TABLE,
    INSERT,
    MERGE_INSERT,
    REGISTER_TABLE,
    DaprEmitter,
    HttpLineageEmitter,
    InputPin,
    InputRef,
    LineageEmitter,
    NoopEmitter,
    build_write_event,
    emit_write_event,
    make_emitter,
    shape_run_facets,
)
from lineage.models import RunEvent
from lineage.services.repository import _CREATE_OPS


def test_build_create_event_shape() -> None:
    # The create event is build_write_event with operation=create_table (the real production path —
    # emit_create → emit_write → build_write_event); this pins that shape.
    event = build_write_event(
        table_id="alpha$bronze$images",
        namespace="alpha$bronze",
        author="alice",
        version=1,
        operation=CREATE_TABLE,
        run_id="r1",
        event_time="2026-06-24T00:00:00+00:00",
        job_namespace="lance-catalog",
    )
    assert event["eventType"] == "COMPLETE"
    output = event["outputs"][0]
    assert output["namespace"] == "alpha$bronze"
    assert output["name"] == "alpha$bronze$images"
    # #20: the standard version facet rides the output so the WROTE edge carries the Lance version.
    assert output["facets"]["version"]["datasetVersion"] == "1"
    # Custom facets carry the spec-required _producer + _schemaURL alongside their payload.
    author = event["run"]["facets"]["author"]
    assert author["name"] == "alice" and author["sub"] == "alice"
    assert author["_producer"] and author["_schemaURL"]
    lance = event["run"]["facets"]["lance"]
    assert lance["operation"] == "create_table" and lance["version"] == 1
    assert lance["_producer"] and lance["_schemaURL"]
    # Top-level schemaURL is present (spec-required on every RunEvent).
    assert event["schemaURL"]
    # Per-table job identity (not the bare op) so one table's writes don't lump into a shared Job node.
    assert event["job"] == {"namespace": "lance-catalog", "name": "create_table.alpha$bronze$images"}


def test_build_write_event_stamps_the_project_so_watchers_can_be_found() -> None:
    """THE WATCH DEFECT. `lance_fields` carried `operation` + `version` and nothing else, so every
    catalog-authored event reached ZERO watchers estate-wide.

    `notifications/api/lineage_events.py::project_id` reads `run.facets.lance.project`, and
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
    assert event["run"]["facets"]["lance"]["project"] == "acme"
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
    assert "project" not in event["run"]["facets"]["lance"]
    assert project_id(LineageRunEvent.model_validate(event).run) is None


def test_build_write_event_attaches_schema_facet_and_round_trips() -> None:
    # #24 coverage fix: a catalog write now carries the per-version column schema (blob/vector-aware) as the
    # standard SchemaDatasetFacet, so the lineage consumer materialises real columns — previously the catalog
    # emitted NO schema and a catalog-created table showed empty columns until a compute job re-asserted it.
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
    schema = event["outputs"][0]["facets"]["schema"]
    assert schema["fields"] == fields
    assert schema["_producer"] and schema["_schemaURL"].endswith("SchemaDatasetFacet")
    # The lineage model reads it back off the standard facet → real per-version columns on the WROTE edge.
    parsed = RunEvent.model_validate(event)
    assert [f.model_dump(exclude_none=True) for f in parsed.outputs[0].fields] == fields


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
    assert "schema" not in event["outputs"][0].get("facets", {})


def test_emit_write_event_forwards_schema_fields() -> None:
    em = _RecordingEmitter()
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["db", "t"],
            delimiter="$",
            author="alice",
            version=2,
            operation=INSERT,
            authorization=None,
            schema_fields=[{"name": "x", "type": "int64"}],
        )
    )
    assert em.writes[0]["schema_fields"] == [{"name": "x", "type": "int64"}]


def test_build_create_event_without_author_omits_facet() -> None:
    event = build_write_event(
        table_id="t",
        namespace="",
        author=None,
        version=1,
        operation=CREATE_TABLE,
        run_id="r1",
        event_time="t",
        job_namespace="lance-catalog",
    )
    assert "author" not in event["run"]["facets"]
    assert event["run"]["facets"]["lance"]["operation"] == "create_table"


def test_create_operation_strings_are_shared() -> None:
    # The catalog emitter and the lineage repository must agree on which facet operations key a CREATED
    # edge — create/register/declare are all "the table came into existence" events (wire contract).
    assert CREATE_TABLE == "create_table"
    assert sorted(_CREATE_OPS) == ["create_table", "declare_table", "register_table"]
    assert CREATE_TABLE in _CREATE_OPS
    assert REGISTER_TABLE in _CREATE_OPS
    assert DECLARE_TABLE in _CREATE_OPS


def test_emitted_event_round_trips_into_lineage_model() -> None:
    """The event the catalog emits must parse in the lineage service's RunEvent model."""
    event = build_write_event(
        table_id="alpha$bronze$images",
        namespace="alpha$bronze",
        author="alice",
        version=1,
        operation=CREATE_TABLE,
        run_id="r1",
        event_time="2026-06-24T00:00:00+00:00",
        job_namespace="lance-catalog",
    )
    parsed = RunEvent.model_validate(event)
    assert parsed.operation == "create_table"
    assert parsed.author == "alice"
    assert parsed.outputs[0].name == "alpha$bronze$images"
    # #20: the version the lineage service folds onto the WROTE edge (was None before this fix).
    assert parsed.output_version("alpha$bronze$images") == "1"


def test_noop_emitter_does_nothing() -> None:
    assert asyncio.run(NoopEmitter().emit_create(table_id="t", namespace="", author=None, version=1)) is None


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
    output = client.posted["outputs"][0]
    assert output["namespace"] == "a"
    assert output["name"] == "a$b"
    assert output["facets"]["version"]["datasetVersion"] == "3"  # #20
    assert client.posted["run"]["facets"]["author"]["sub"] == "alice"


def test_the_dapr_emitter_STAGES_the_event_rather_than_publishing_it_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog's emit is the cascade HEAD, and losing one does not merely dent provenance.

    docs/RESILIENCE.md gap #1 calls this the estate's #1 weakness: the emit is inline-awaited and
    best-effort AFTER the Lance write commits, so a crash between the write and the publish loses the
    event. The data exists on storage, the graph never learns of it — and because medallion's
    `/bronze-arrival` subscription reacts to this very announcement, the whole bronze->silver->gold
    run silently never happens. The doc names the transactional outbox as what "closes the window
    fully", and this asserts the catalog now goes through it.

    The invariants ratchet (`test_the_set_of_bare_lineage_publishes_does_not_grow`) proves no BARE
    publish site remains, which is the structural half — but it would pass just as well if the emit
    had been deleted outright. This is the other half: it still publishes, and it publishes STAGED.
    """
    import asyncio as _asyncio

    from catalog.core import lineage_emit as module

    calls: list[dict[str, object]] = []

    async def _fake_outbox(_publisher: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(module.outbox, "publish_lineage_with_outbox", _fake_outbox)
    emitter = DaprEmitter(
        cast("Any", object()),
        "pubsub",
        "lineage.events.v1",
        job_namespace="lance-catalog",
        timeout_seconds=5.0,
        outbox_uri="s3://staging/outbox",
        storage_options={"region": "eu-north-1"},
    )
    _asyncio.run(emitter.emit_create(table_id="a$b", namespace="a", author="alice", version=1, run_id="r-1"))

    assert len(calls) == 1, "the catalog emit did not reach the outbox"
    staged = calls[0]
    assert staged["outbox_uri"] == "s3://staging/outbox"
    assert staged["storage_options"] == {"region": "eu-north-1"}
    assert staged["topic_name"] == "lineage.events.v1"
    # The run id is what the staged object is keyed on, so a wrong one stages under a name the relay
    # cannot find — the loss this fix exists to prevent, arriving one layer down.
    assert staged["run_id"] == "r-1"


def test_http_emitter_uses_shared_run_id() -> None:
    # #21: the catalog passes the same run id it stamped into the Lance file, so the file points at
    # its exact creating run in the lineage graph.
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage/api/v1/lineage", job_namespace="lance-catalog")
    asyncio.run(emitter.emit_create(table_id="a$b", namespace="a", author="alice", version=1, run_id="r-shared"))
    assert client.posted is not None
    assert client.posted["run"]["runId"] == "r-shared"


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
            delimiter="$",
            author="alice",
            version=1,
            operation=INSERT,
            authorization=None,
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
            delimiter="$",
            author="alice",
            version=3,
            operation="update",
            authorization="Bearer x",
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
    assert w["run_id"]  # a fresh run id is generated per emit


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
    assert event["outputs"][0]["name"] == "db1$renamed"


def test_build_write_event_default_has_no_inputs() -> None:
    event = build_write_event(
        table_id="db1$t",
        namespace="db1",
        author=None,
        version=1,
        operation="create_table",
        run_id="r1",
        event_time="2026-07-15T00:00:00Z",
        job_namespace="catalog",
    )
    assert event["inputs"] == []  # a fresh write is derived from nothing


def test_merge_insert_event_carries_version_pinned_input_and_passed_run_facet() -> None:
    # Phase 2: a mover's merge from source@N emits training-shaped OpenLineage — a version-PINNED INPUT
    # (the standard DatasetVersionDatasetFacet, i.e. the reproducibility pin the lineage service reads via
    # input_version) plus a caller-supplied run facet the catalog carries VERBATIM (un-opinionated: it only
    # stamps it spec-legal, it does not interpret the payload).
    facets = shape_run_facets({"params": {"lr": 0.01, "epochs": 5}})
    event = build_write_event(
        table_id="db$gold",
        namespace="db",
        author="alice",
        version=7,
        operation=MERGE_INSERT,
        run_id="r1",
        event_time="2026-07-21T00:00:00Z",
        job_namespace="catalog",
        inputs=[InputRef("db", "db$silver", 4)],
        extra_run_facets=facets,
    )
    # The INPUT dataset carries the standard DatasetVersionDatasetFacet pinning the consumed source version.
    assert len(event["inputs"]) == 1
    source = event["inputs"][0]
    assert source["namespace"] == "db" and source["name"] == "db$silver"
    version_facet = source["facets"]["version"]
    assert version_facet["datasetVersion"] == "4"
    assert version_facet["_producer"] and version_facet["_schemaURL"].endswith("DatasetVersionDatasetFacet")
    # The passed run facet rides verbatim on the run, stamped spec-legal (custom_facet _producer/_schemaURL).
    params = event["run"]["facets"]["params"]
    assert params["lr"] == 0.01 and params["epochs"] == 5
    assert params["_producer"] and params["_schemaURL"]
    # Adversarial round-trip: the lineage RunEvent model reads the pin back off the standard facet, so the
    # graph really can answer "which exact source version produced this merge?" (#115 reproducibility).
    parsed = RunEvent.model_validate(event)
    assert parsed.input_version("db$silver") == "4"


@pytest.mark.parametrize(
    "raw",
    [
        {"author": {"name": "admin", "sub": "admin"}},  # forge the verified principal
        {"lance": {"operation": "create_table"}},  # forge the op → a false CREATED edge
        {"errorMessage": {"message": "x"}},  # forge run state the consumer trusts
        {"progress": {"percent": 100}},
        {"parent": {"run": {}}},
        {"": {"k": "v"}},  # a nameless facet
    ],
)
def test_shape_run_facets_rejects_reserved_facet_names(raw: dict[str, Any]) -> None:
    # A producer may NOT set a catalog-owned / consumer-trusted facet name — else it forges the author, the
    # operation (a false CREATED edge), or run state on the governed graph (the Phase 2 audit blocker).
    with pytest.raises(ValueError, match="reserved"):
        shape_run_facets(raw)


def test_shape_run_facets_rejects_reserved_payload_keys_and_non_objects() -> None:
    # The catalog owns the spec's _-prefixed fields AND custom_facet's `producer` positional (a `producer`
    # key would raise a bare TypeError → a 500); each payload must be a JSON object. All fail-fast as 4xx.
    with pytest.raises(ValueError, match="reserved"):
        shape_run_facets({"params": {"_producer": "evil"}})
    with pytest.raises(ValueError, match="reserved"):
        shape_run_facets({"params": {"producer": "evil"}})  # would otherwise be a TypeError → 500
    with pytest.raises(ValueError, match="JSON object"):
        shape_run_facets({"params": ["not", "an", "object"]})


def test_build_write_event_catalog_facets_win_over_caller_facets() -> None:
    # Defense in depth: even if a caller-supplied `lance`/`author` facet reached build_write_event (past
    # shape_run_facets), the catalog's stamp is applied AFTER and MUST win — no author/operation forgery.
    forged = {
        "lance": {"_producer": "x", "_schemaURL": "x", "operation": "create_table", "version": 999},
        "author": {"_producer": "x", "_schemaURL": "x", "name": "admin", "sub": "admin"},
    }
    event = build_write_event(
        table_id="db$t",
        namespace="db",
        author="mallory",
        version=3,
        operation=MERGE_INSERT,
        run_id="r1",
        event_time="2026-07-21T00:00:00Z",
        job_namespace="catalog",
        extra_run_facets=forged,
    )
    assert event["run"]["facets"]["lance"]["operation"] == "merge_insert"  # catalog op wins, not create_table
    assert event["run"]["facets"]["author"]["sub"] == "mallory"  # verified principal wins, not admin
    parsed = RunEvent.model_validate(event)
    assert parsed.operation == "merge_insert" and parsed.author == "mallory"


def test_emit_write_event_maps_input_pins_to_canonical_dataset_ids() -> None:
    # The rename handler passes the SOURCE segments as an InputPin; emit_write_event must resolve them to the
    # SAME canonical (namespace, id) the rest of the graph keys on, so the DERIVED_FROM edge points at the
    # real source node. An unpinned source carries version=None (no reproducibility facet).
    em = _RecordingEmitter()
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["db1", "dest"],
            delimiter="$",
            author="alice",
            version=None,
            operation="register_table",
            authorization=None,
            inputs=[InputPin(segments=["db1", "src"])],
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
            delimiter="$",
            author="alice",
            version=9,
            operation=MERGE_INSERT,
            authorization=None,
            inputs=[InputPin(segments=["db1", "src"], version=5)],
        )
    )
    assert em.writes[0]["inputs"] == [InputRef("db1", "db1$src", 5)]  # the pin survives resolution


def test_emit_write_event_root_table_has_empty_namespace() -> None:
    # Boundary: a top-level (single-segment) table has no parent namespace → "".
    em = _RecordingEmitter()
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["t"],
            delimiter="$",
            author=None,
            version=None,
            operation=INSERT,
            authorization=None,
        )
    )
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
    assert "version" not in event["outputs"][0].get("facets", {})  # no version facet for a drop
    assert event["run"]["facets"]["lance"]["operation"] == "drop_table"
    assert "version" not in event["run"]["facets"]["lance"]
    assert event["job"]["name"] == "drop_table.db$t"


def test_emit_write_event_deregister_is_versionless_marker() -> None:
    # deregister detaches without deleting data — recorded as a versionless marker (like drop) so the
    # Dataset node isn't left looking like a live, never-touched table. version=None asserts no Lance write.
    em = _RecordingEmitter()
    asyncio.run(
        emit_write_event(
            cast(LineageEmitter, em),
            ["db", "t"],
            delimiter="$",
            author="alice",
            version=None,
            operation=DEREGISTER_TABLE,
            authorization=None,
        )
    )
    w = em.writes[0]
    assert w["operation"] == "deregister_table"
    assert w["version"] is None
    assert w["table_id"] == "db$t"


def test_http_emitter_omits_auth_header_when_absent() -> None:
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage", job_namespace="lance-catalog")
    asyncio.run(emitter.emit_create(table_id="a$b", namespace="a", author="alice", version=1))
    assert client.headers is None


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
    asyncio.run(emitter.emit_create(table_id="a$b", namespace="a", author="alice", version=1, run_id="r-1"))
    assert len(dapr.published) == 1
    pub = dapr.published[0]
    assert pub["pubsub"] == "lineage-pubsub" and pub["topic"] == "lineage.events.v1"
    assert pub["content_type"] == "application/json"
    assert pub["event"]["outputs"][0]["name"] == "a$b"  # the spec-correct OpenLineage event is the data
    assert pub["event"]["run"]["facets"]["author"]["sub"] == "alice"  # the verified catalog author
    assert pub["event"]["run"]["runId"] == "r-1"


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
    assert event["run"]["facets"]["lance"]["operation"] == "insert"
    assert "version" not in event["run"]["facets"]["lance"]  # no version key on a version-less insert
    assert "facets" not in event["outputs"][0]  # no version facet asserted when version is None


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
    assert event["run"]["facets"]["lance"]["operation"] == "merge_insert"
    assert event["run"]["facets"]["lance"]["version"] == 4
    assert event["outputs"][0]["facets"]["version"]["datasetVersion"] == "4"
    assert "author" not in event["run"]["facets"]


def test_write_event_round_trips_into_lineage_model() -> None:
    event = build_write_event(
        table_id="a$b",
        namespace="a",
        author="alice",
        version=None,
        operation=INSERT,
        run_id="r1",
        event_time="2026-06-24T00:00:00+00:00",
        job_namespace="lance-catalog",
    )
    parsed = RunEvent.model_validate(event)
    assert parsed.operation == "insert"
    assert parsed.is_success is True
    assert parsed.output_version("a$b") is None  # an insert asserts no Lance version on the WROTE edge


def test_http_emitter_emit_write_posts_operation_and_version() -> None:
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage/api/v1/lineage", job_namespace="lance-catalog")
    asyncio.run(emitter.emit_write(table_id="a$b", namespace="a", author="alice", version=4, operation=MERGE_INSERT, run_id="r-9"))
    assert client.posted is not None
    assert client.posted["job"]["name"] == "merge_insert.a$b"
    assert client.posted["run"]["runId"] == "r-9"
    assert client.posted["outputs"][0]["facets"]["version"]["datasetVersion"] == "4"


@pytest.mark.asyncio
async def test_a_catalog_write_RESOLVES_its_tenant_so_watchers_are_reachable() -> None:
    """Trap 3, closed at the one place that can close it.

    `lance.project` is what the notifications plane's WATCH fan-out keys on: `fanout.py` skips the
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

    lance = client.posted["run"]["facets"]["lance"]
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

    assert client.posted["run"]["facets"]["lance"]["project"] == "acme"


@pytest.mark.asyncio
async def test_an_UNRESOLVABLE_tenant_emits_exactly_as_before() -> None:
    """Best-effort, like the emit itself: this runs on a COMMITTED write, so a registry blip must cost
    the watchers their notification, never the caller their request. No resolver → no project key,
    byte-identical to the pre-existing behaviour."""
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage", job_namespace="lance-catalog")

    await emitter.emit_create(table_id="db$t", namespace="db", author="alice", version=1)

    assert "project" not in client.posted["run"]["facets"]["lance"]


@pytest.mark.asyncio
async def test_a_SERVICE_run_can_name_the_person_it_runs_FOR() -> None:
    """Q2 — ORIGINATOR — for the catalog's own emits.

    Some catalog writes are made BY a service ON BEHALF OF a person: the annotator's publish saga is
    the live case, driven from an actor reminder under a dedicated `publisher@rask.internal` service
    account. `enforce_author` correctly overwrites `author` with that service's verified sub, so the
    human CANNOT be the author and must not try to be — `lance.originator` is the field for exactly
    this, and the catalog had no way to carry it.

    It is a TARGETING hint and authorizes nothing: the notifications plane re-derives every
    recipient's visibility at delivery, so a forged one can at worst put a row in an inbox whose
    owner can already see the run's outputs.
    """
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage", job_namespace="lance-catalog")

    await emitter.emit_create(
        table_id="acme-silver$annotations", namespace="acme-silver", author="service-publisher", version=1, originator="CiQwOGE4Njg0Yi1kYjg4"
    )

    lance = client.posted["run"]["facets"]["lance"]
    assert lance["originator"] == "CiQwOGE4Njg0Yi1kYjg4"
    assert client.posted["run"]["facets"]["author"]["sub"] == "service-publisher", "the service remains the author; originator is additive"


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", ["", "*", "user:*", "team:acme#member", "system"])
async def test_a_NON_PERSONAL_originator_is_DROPPED(junk: str) -> None:
    """Trap 4. An address must identify a person; a role, a wildcard or a userset is not one, and
    carrying it writes into an inbox actor literally named that."""
    client = _CapturingClient()
    emitter = HttpLineageEmitter(cast(httpx.AsyncClient, client), "http://lineage", job_namespace="lance-catalog")

    await emitter.emit_create(table_id="db$t", namespace="db", author="svc", version=1, originator=junk)

    assert "originator" not in client.posted["run"]["facets"]["lance"]
