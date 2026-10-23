"""Best-effort OpenLineage emission from the catalog to the lineage service.

The catalog is the only component that knows the *verified* principal on every write, so it is
the authoritative source of "who created/changed a table". On a table create it emits an
OpenLineage ``RunEvent`` (output = the table, ``author`` = the token sub, plus a ``lance`` run
facet naming the operation + version) to the lineage service's ingest endpoint.

Emission is **inline-awaited + best-effort**: the write endpoints ``await`` it (NOT a FastAPI
BackgroundTasks fire-and-forget — that dies with the worker + can't reach the durable transport before
the response) but it swallows every error, so the lineage service being down/slow can never block or
fail a catalog write. Two transports sit behind the same :class:`LineageEmitter` interface:

* :class:`HttpLineageEmitter` — direct HTTP POST (the OpenLineage default transport; simple, but the
  event is lost if the lineage service is down when we POST). Good for dev / external producers.
* :class:`DaprEmitter` — publish to the **Dapr** ``pubsub.jetstream`` component (the production
  transport, ``LANCE_LINEAGE_TRANSPORT=dapr``). We publish to our local Dapr sidecar; the sidecar
  persists to NATS JetStream and owns retry/backoff/trace-propagation as **component config** (no broker
  client in app code) — the decoupled microservice path. A delivery the subscriber can't process after its
  retry budget dead-letter-parks on a ``dlq.*`` topic (Dapr-native DLQ, default-on via the
  ``dapr.resiliency.enabled`` chart resiliency; the subscriber's ``/dlq-event`` route ERROR-logs + acks —
  park-and-alert, not replay — docs/RESILIENCE.md gap #2, fixed 2026-07-12). The lineage
  service subscribes via its own sidecar. With ``LANCE_LINEAGE_OUTBOX_URI`` set the event is staged in the
  object-store outbox (``service_kit.lakehouse.outbox``) before the publish, which narrows the loss window
  to commit→stage: a crash between the Lance write and the stage still loses it.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass  # noqa: TID251
from datetime import UTC, datetime
from typing import Any, NamedTuple, Protocol, TypedDict, Unpack, runtime_checkable

import httpx
import lance
from dapr.aio.clients import DaprClient
from opentelemetry import metrics
from pydantic import BaseModel

from catalog.core.run_context import RunContext
from lineage_kit import CanonError, SigningKey, attach_signature, author_of
from service_kit.governed import fga
from service_kit.governed.signing_key import SigningKeyHolder, SigningKeyUnavailableError
from service_kit.lakehouse import outbox
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.schema import SchemaFields
from service_kit.lakehouse.subjects import is_person_subject
from service_kit.lakehouse.warehouse_registry import is_safe_project
from service_kit.openlineage import (
    DATASET_EVENT_SCHEMA_URL,
    DATASOURCE_FACET_SCHEMA_URL,
    LANCE_FACET,
    RUN_EVENT_SCHEMA_URL,
    VERSION_FACET_SCHEMA_URL,
    DatasetUuids,
    catalog_facet,
    custom_facet,
    dataset_type_facet,
    event_identity,
    lifecycle_facet,
    lifecycle_state,
    processing_engine_facet,
    schema_facet,
    with_dataset_uuids,
)


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_emit_failed = _meter.create_counter(
    "catalog.lineage_emit.failed",
    unit="{event}",
    description="Best-effort catalog lineage emits that failed terminally, by transport. On `http` the "
    "event is simply gone; on `dapr` the publish goes through the staged "
    "outbox, so a failure AFTER staging leaves a durable copy the relay can still drain and a failure "
    "at staging does not. The counter cannot tell those apart, which is why any increase is worth a look.",
)

#: Operation markers carried in the OpenLineage ``lance`` run facet. Lineage's ingest authorizer reads them to
#: pick the relation a write needs and to admit a drop (``lineage.api.fga_deps``), and the notifications plane
#: renders them, so the producers and those readers share these contract strings.
CREATE_TABLE = "create_table"
INSERT = "insert"
MERGE_INSERT = "merge_insert"
UPDATE = "update"
DELETE = "delete"
#: A table drop — recorded as a versionless run on the dataset (it has no version after the drop). The feed
#: keeps it as a historical provenance record, as a forwarded graph backend keeps the dropped dataset (Marquez
#: does); the operation names it a drop so a reader can tell it was deleted, not just last-written.
DROP_TABLE = "drop_table"
#: A table deregister — detaches the table from the catalog WITHOUT deleting its data. Recorded as a
#: versionless run (asymmetric with drop, which deletes) so the detach has a provenance marker instead of
#: leaving the dataset looking like a still-live, never-touched table.
DEREGISTER_TABLE = "deregister_table"
#: Schema-evolution ops. Each bumps the Lance version; add/alter/drop change the column set, so their event
#: carries the NEW per-version schema facet, and a reader of the feed or a forwarded graph backend follows the
#: evolution instead of freezing at create-time. ``update_field_metadata`` / ``update_schema_metadata`` bump the
#: version without changing columns but still record a versioned write for provenance completeness.
ADD_COLUMNS = "add_columns"
ALTER_COLUMNS = "alter_columns"
DROP_COLUMNS = "drop_columns"
UPDATE_FIELD_METADATA = "update_field_metadata"
UPDATE_SCHEMA_METADATA = "update_schema_metadata"
#: Index lifecycle. Building/dropping an index bumps the Lance version (new manifest) without touching data
#: or schema; recorded so provenance shows when a scalar/vector index was (re)built or removed.
CREATE_INDEX = "create_index"
DROP_INDEX = "drop_index"
#: A compaction landed: fragments were merged and the table got a new version. Recorded for the same
#: reason the index ops are — the version moved and an operator asking "what touched this table at
#: 03:00?" must get an answer — but kept DISTINCT from the data ops on purpose. A compaction changes
#: no row, so folding it into ``insert``/``update`` would put a content change in the feed where none
#: happened, and a consumer replaying the writes would see phantom writes on every maintenance pass.
COMPACT_TABLE = "compact_table"
#: Restore moves the table's current version to a prior one — a real version-state change, recorded as a
#: versioned write at the new (restored) version.
RESTORE_TABLE = "restore_table"
#: A create or an insert in Overwrite mode that replaced an EXISTING table's contents ([[LH-242]]): a new
#: version of the same table, its history and grants intact. Distinct from ``create_table`` because no table
#: came into being, and from ``insert`` because the rows before it are gone from the tip; it carries the standard ``OVERWRITE`` lifecycle state.
OVERWRITE_TABLE = "overwrite_table"
#: Declare reserves a table id with no data yet (versionless); register attaches an existing storage
#: location. Both are "the table came into existence in this catalog" events, like ``create_table``.
DECLARE_TABLE = "declare_table"
REGISTER_TABLE = "register_table"
#: #17 model promotion: the ``blessed`` tag on a model registry (``models$<model>``) was moved to a candidate
#: Lance version (candidate→blessed). A metadata-only tag move mints no new data version, so the emitted
#: ``version`` is the tag's TARGET (the promoted model version), not a fresh write — a distinct op so a
#: blessing is never mistaken for a training run or a data write on the run board.
PROMOTE_MODEL = "promote_model"
#: The tag gate (ADR 0156 C1, [[LH-466]]): a table's ``published`` tag moved, so readers of its tier now read
#: ``version``, the tag's target. Like a model promotion it mints no version, and it is a run of its own so the
#: project's watchers are told that new data is live: the notifications plane targets them by this event's
#: ``lance.project`` (`.claude/skills/rask-notifications`, WATCH).
PUBLISH_TABLE = "publish_table"

#: OpenLineage ``producer`` URI — identifies the software that emitted the event (spec-required,
#: and what a Marquez-style consumer records as the event source).
_PRODUCER = "https://github.com/AI-Riksarkivet/rask/tree/main/services/catalog/src/catalog/core/lineage_emit.py"

#: OpenLineage standard ``DatasetVersionDatasetFacet`` schema URL. The output dataset carries this
#: facet so the lineage service records the Lance version on every edge the write asserts
#: (``lineage.services.provenance.edges_of`` reads ``outputs[].facets.version.datasetVersion``) — without it
#: the edge is versionless (the custom ``lance`` run facet is not read for version).
_VERSION_FACET_SCHEMA = VERSION_FACET_SCHEMA_URL

#: OpenLineage standard ``DatasourceDatasetFacet`` schema URL. The output dataset carries this facet with
#: the **physical storage URI**, the standard field a graph backend the feed is forwarded to records as where
#: the dataset lives.
_DATASOURCE_FACET_SCHEMA = DATASOURCE_FACET_SCHEMA_URL


class UuidReader(Protocol):
    """Reads a table's uuid and its namespace's for an event (``catalog.api.dependencies.ObjectUuids``): ``uuid`` when the
    door resolved the table, else the claim at ``location``, else the table its name names now. Never raises."""

    async def table(self, segments: list[str], *, location: str | None = None, uuid: str | None = None) -> DatasetUuids: ...


class InputPin(BaseModel):
    """A source dataset an emitted write derives from, with the EXACT version it consumed (or ``None``).

    The API-surface shape (catalog ``segments``, as any ``{id}`` route uses); ``emit_write_event`` resolves
    it to the canonical lineage/FGA id. A pinned ``version`` becomes a ``DatasetVersionDatasetFacet`` on the
    input edge — the reproducibility handshake a derived write (a pipeline's merge_insert from ``source@N``)
    needs so its provenance names the precise input version, not just the dataset.
    """

    segments: list[str]
    version: int | None = None
    #: The branch the version is on, ``None`` for main: a branch numbers its versions apart from main.
    branch: str | None = None
    #: The table's uuid when the door already resolved the name, as a rename has its source's: the name may name
    #: nothing by the time the event is built.
    uuid: str | None = None


class InputRef(NamedTuple):
    """A resolved input edge for the wire builder: canonical ``(namespace, name)``, pinned version, its branch and its uuids."""

    namespace: str
    name: str
    version: int | None
    branch: str | None = None
    uuids: DatasetUuids = DatasetUuids()


async def input_refs(pins: list[InputPin] | None, *, uuids: UuidReader) -> list[InputRef] | None:
    """``pins`` resolved to the canonical ids FGA keys on and the uuids the edge list keys on, so an edge names the real table."""
    refs = [
        InputRef(
            fga.parent_namespace_id(pin.segments) or "",
            fga.canonical_object_id(pin.segments),
            pin.version,
            pin.branch,
            await uuids.table(pin.segments, uuid=pin.uuid),
        )
        for pin in pins or []
    ]
    return refs or None


def run_inputs(run: RunContext | None) -> list[InputPin] | None:
    """The inputs a run names, as the pins a write's event derives from."""
    if run is None or not run.inputs:
        return None
    return [InputPin(segments=list(entry.table), version=entry.version, branch=entry.branch) for entry in run.inputs]


def _input_dataset(ref: InputRef) -> dict[str, Any]:
    """An OpenLineage INPUT dataset, carrying the standard ``DatasetVersionDatasetFacet`` when a source
    version is pinned, and rask's ``lance`` facet naming the branch that version is on."""
    dataset: dict[str, Any] = {"namespace": ref.namespace, "name": ref.name}
    if ref.version is not None:
        dataset["facets"] = {
            "version": {
                "_producer": _PRODUCER,
                "_schemaURL": _VERSION_FACET_SCHEMA,
                "datasetVersion": str(ref.version),
            }
        }
    if ref.branch is not None:
        dataset.setdefault("facets", {})["lance"] = custom_facet(_PRODUCER, ref=ref.branch)
    return with_dataset_uuids(dataset, ref.uuids, producer=_PRODUCER)


#: Principals that are not an address: a wildcard names everyone, a role names a job.
#: Values that name a role, a machine, or everyone — never one person. `anon` is the estate-wide
#: ANONYMOUS_SUBJECT (`service_kit.governed.deps`): with OIDC off every verified-subject dependency
#: resolves to it, so without this entry a dev or auth-off estate would address one shared inbox
#: actor literally named `anon` on behalf of everybody.


#: `is_person_subject` is imported from service-kit rather than defined here: it has more than one
#: producer, and the medallion — whose authors ARE chart role literals — cannot depend on the catalog,
#: so a rule written here could only ever be applied here. See that module for why that matters.
def _is_ddl(operation: str) -> bool:
    """Whether this operation CHANGED THE TABLE'S DEFINITION rather than its rows.

    Read off `_LIFECYCLE_BY_OPERATION` rather than restated: that map already answers "is this DDL" for
    the standard lifecycle facet, and a second list is a second place for a new operation to be
    forgotten — it would keep emitting a run, and the phantom it mints is invisible until someone
    counts the feed's runs.

    ``OVERWRITE`` is not DDL here: an overwrite committed rows at a version, and only a run's output names
    the version a write made (``docs/COMMIT-EVENTS.md``). A create that committed rows is the same case, but
    the operation alone cannot say so: :func:`build_write_event` reads ``commits_rows`` for it.
    """
    state = lifecycle_state(operation)
    return state is not None and state != "OVERWRITE"


def _as_dataset_event(*, output: dict[str, Any], run_facets: dict[str, Any], event_time: str) -> dict[str, Any]:
    """The same write, shaped as the spec's ``DatasetEvent``: no run, no job, no event type.

    THE RUN FACETS MOVE ONTO THE DATASET, they are not dropped. `author` and `lance` are rask's OWN
    custom facets rather than spec-typed run facets, and two readers depend on them: the ingest
    authorizer takes `author.sub` (and refuses the standard `ownership` facet, so there is nowhere else
    for it to go) and `lance.operation` (the relation the change needs, and whether it is a drop), and
    the notifications plane takes `author.sub`, `lance.project`, `lance.originator` and `lance.operation`. Leaving them on
    a run that no longer exists would silently unauthor every DDL change and silence the person it
    belongs to.

    `processingEngine` DOES NOT MOVE, and the omission is the spec rather than an oversight:
    `ProcessingEngineRunFacet` is typed for a run, and this event has none. Nothing is lost that a
    reader can act on — the `catalog` facet already names who governs this table, and for a DDL change
    the engine is always the catalog committing in-process.
    """
    facets = dict(output.get("facets") or {})
    for name in ("author", "lance"):
        if name in run_facets:
            facets[name] = run_facets[name]
    # The dataset's own `lance` facet names the table's uuids (ADR 0173 §10); the run's joins it rather than replacing it.
    if LANCE_FACET in run_facets and isinstance(own := (output.get("facets") or {}).get(LANCE_FACET), dict):
        facets[LANCE_FACET] = {**own, **run_facets[LANCE_FACET]}
    return {
        "eventTime": event_time,
        "producer": _PRODUCER,
        "schemaURL": DATASET_EVENT_SCHEMA_URL,
        "dataset": {"namespace": output["namespace"], "name": output["name"], "facets": facets},
    }


def build_write_event(
    *,
    table_id: str,
    namespace: str,
    author: str | None,
    version: int | None,
    operation: str,
    run_id: str,
    event_time: str,
    job_namespace: str,
    source_uri: str | None = None,
    schema_fields: SchemaFields | None = None,
    inputs: list[InputRef] | None = None,
    run: RunContext | None = None,
    project: str | None = None,
    originator: str | None = None,
    branch: str | None = None,
    branch_identifier: str | None = None,
    commits_rows: bool = False,
    catalog_impl: str = "",
    warehouse_uri: str = "",
    uuids: DatasetUuids | None = None,
) -> dict[str, Any]:
    """Build the OpenLineage ``RunEvent`` (wire JSON) for any catalog write to a table.

    ``table_id`` is the catalog's canonical id (e.g. ``alpha$bronze$images``), the lineage ``Dataset`` name a caller of
    the doors reads; the table's identity across a rename is the uuid ``uuids`` carries in the output's ``lance`` facet,
    which its OpenFGA object (``table:<uuid>``) names (ADR 0173 §8, §10). ``operation`` is the catalog op (``create_table`` / ``insert`` / ``merge_insert``
    / ``update`` / ``delete``). ``version`` is the Lance version the write produced; when it is ``None``
    (e.g. an insert whose response carries no version) the standard version facet is omitted so the
    event records the run without asserting a version. ``run_id`` / ``event_time`` are injected
    so the builder is pure and deterministically testable.

    ``inputs`` names the ``(namespace, table_id)`` datasets this write is DERIVED FROM — a rename passes the
    SOURCE table so the destination's provenance chain is not severed (the edge list holds dest←source instead
    of the renamed table appearing as an orphan with no history). Default ``None`` → no input edge, the
    normal case for a fresh write.

    ``run`` is the run the writer named (:mod:`catalog.core.run_context`); its id and job ride the catalog's ``lance``
    facet, because ``run_id`` is this event's own id and the writer's need not be the UUID OpenLineage asks for.

    ``commits_rows`` says the commit added rows ([[LH-492]]): a table's first version can, through the catalog's
    ``create`` door or a stock client's ``version/create``. Such a create is announced as a run whose output names
    the version and whose author is the caller, as an Overwrite is, because only a run's output carries a write.

    ``uuids`` are the table's and its namespace's (ADR 0173 §10): the output's ``lance`` facet carries them beside its
    name, as each input's carries its own, so lineage keys the table by what a rename keeps.
    """
    lance_fields: dict[str, Any] = {"operation": operation}
    if run is not None:
        lance_fields["runId"] = run.run_id
        if run.job is not None:
            lance_fields["job"] = run.job
    if version is not None:
        lance_fields["version"] = version
    # THE REF THE WRITE LANDED ON, because the version alone does not identify a snapshot. A branch is a
    # whole parallel dataset under `tree/<name>/` with its OWN version sequence
    # (`core/namespace.py::open_dataset`), so one number names two different states. Measured on the
    # installed pylance: main at v2, a branch write gives branch v3, a later main write gives main v3 —
    # same dataset, same number, different contents. The edge list records the ref beside the version
    # (`out_branch`), so `(table, ref, version)` names one commit.
    #
    # ABSENT for main rather than the literal "main": every historical write carries no ref, so a literal
    # would make them all look like a different one, and a consumer asking "was this a branch write?" by
    # testing presence would get yes for everything.
    if branch:
        lance_fields["ref"] = branch
        # WHICH INCARNATION of that ref. A deleted and recreated branch restarts its numbering, so
        # `(table, ref, N)` names two different commits; pylance 12 records a fresh `branch_identifier`
        # UUID for each create, and it differs across a delete-and-recreate in the same second while
        # `parentVersion` and `createAt` are identical (measured on 12.0.0). Main has no identifier.
        if branch_identifier:
            lance_fields["branchIdentifier"] = branch_identifier
    # THE TENANT, and it is WATCH targeting's only key. `notifications` reads
    # `run.facets.lance.project` (`api/lineage_events.py::project_id`) and its fan-out skips the
    # watcher loop ENTIRELY when it is absent — so while this facet carried only operation/version,
    # every catalog-authored event in the estate reached zero watchers. Not a broken watch: a
    # producer that never named the tenant the watch is keyed on. `ingest/lineage.py` was the
    # working precedent the whole time.
    #
    # GUARDED, and OMITTED rather than sanitized when it fails. `is_safe_project` is the same check
    # ingest's `tenant()` and the medallion's `_cascade_project` apply, for the reason stated there:
    # a value outside the path-safe shape must never become a lineage-name qualifier. Omitting is the
    # safe direction — a project-less run reaches its author and no watchers, whereas a coerced one
    # could reach the WRONG tenant's watchers, which is a disclosure rather than a miss.
    if project and is_safe_project(project):
        lance_fields["project"] = project
    # `enforce_author` overwrites `author` with the authenticating service's sub, so a service-made
    # write can only name its human here. Targeting only — it authorizes nothing.
    if is_person_subject(originator):
        lance_fields["originator"] = originator
    run_facets: dict[str, Any] = {"lance": custom_facet(_PRODUCER, **lance_fields)}
    # WHICH ENGINE WROTE THIS, in the field a non-rask consumer reads. The catalog commits through
    # pylance in-process while the medallion's stage lanes write through Ray, so "what produced this
    # table" has two answers and, without this facet, no way for a forwarded graph backend to tell them apart. Stamped
    # beside `lance` rather than inside it: `lance.operation` is rask's own vocabulary and opaque to
    # anyone else, and the engine is exactly the part that is not rask-specific.
    run_facets["processing_engine"] = processing_engine_facet(_PRODUCER, name="lance", version=lance.__version__)
    if author is not None:
        run_facets["author"] = custom_facet(_PRODUCER, name=author, sub=author)
    output: dict[str, Any] = {"namespace": namespace, "name": table_id}
    facets: dict[str, Any] = {}
    if version is not None:
        # Standard version facet → the edges this write asserts carry the Lance version (#20).
        facets["version"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _VERSION_FACET_SCHEMA,
            "datasetVersion": str(version),
        }
    if source_uri:
        # Standard dataSource facet → the physical Lance URI, where a graph backend records the dataset lives.
        facets["dataSource"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _DATASOURCE_FACET_SCHEMA,
            "name": source_uri,
            "uri": source_uri,
        }
    # WHAT THIS DID TO THE DATASET, in the spec's own six-value vocabulary. `lance.operation` already
    # carries rask's more specific name and stays — this is the half a standard consumer can read, and
    # without it a create, a drop and an alter are one event with a different opaque string. Empty for
    # a DATA operation, deliberately: see `lifecycle_facet`.
    if lifecycle := lifecycle_facet(_PRODUCER, operation):
        facets["lifecycleStateChange"] = lifecycle
    if schema_fields:
        # Standard schema facet → the per-version column schema (blob/vector-aware) (#24), so a
        # catalog-written table carries its real columns in the feed and in a forwarded graph backend.
        # Built by the SHARED service_kit.openlineage helper — one spec version across all emitters.
        facets["schema"] = schema_facet(_PRODUCER, schema_fields)
    # WHICH CATALOG GOVERNS THIS, so two estates writing `bronze$events` are not one dataset in a shared
    # graph backend. `job_namespace` is reused as the catalog's NAME rather than taking a second parameter:
    # it is already this catalog's identity on every event it emits, and a separate value could drift
    # into the facet and the job disagreeing about who is speaking.
    if catalog := catalog_facet(_PRODUCER, impl=catalog_impl, name=job_namespace, warehouse_uri=warehouse_uri):
        facets["catalog"] = catalog
    # WHAT KIND of thing this is. Unconditionally TABLE: this builder serves CATALOG writes, and the
    # catalog writes governed Lance tables and nothing else — an external source reaches the feed as
    # an INPUT ref, never as something this event wrote.
    facets["datasetType"] = dataset_type_facet(_PRODUCER, external=False)
    if facets:
        output["facets"] = facets
    output = with_dataset_uuids(output, uuids or DatasetUuids(), producer=_PRODUCER)
    # A DDL change that DERIVED the table from another one keeps its run, and the spec is why: a
    # `DatasetEvent` has only `dataset` — no `inputs` — and the edge list builds every edge from a run's
    # inputs (`lineage.services.provenance.edges_of`). A rename passes its SOURCE precisely so the
    # destination is not an orphan with no history, so moving it would sever the chain this event
    # exists to preserve. There is no phantom in that case either: something really did derive one
    # table from another. A definition change that committed rows keeps its run for the same reason an Overwrite
    # does: those rows are a write at this version, and a follower acting on writes reads them off a run.
    if _is_ddl(operation) and not inputs and not commits_rows:
        return _as_dataset_event(output=output, run_facets=run_facets, event_time=event_time)
    return {
        "eventType": "COMPLETE",
        "eventTime": event_time,
        "producer": _PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA_URL,
        "run": {"runId": run_id, "facets": run_facets},
        # Job identity is per-TABLE (``<operation>.<table_id>``), not the bare op — else every table's
        # writes lump into one job (``insert``) whose output set spans every table, in the feed and in a
        # forwarded graph backend. Per-table keeps a job's output set scoped to the one table it wrote.
        "job": {"namespace": job_namespace, "name": f"{operation}.{table_id}"},
        "inputs": [_input_dataset(ref) for ref in (inputs or [])],
        "outputs": [output],
    }


#: How the emitter learns which TENANT a write belongs to, given the table's TOP namespace segment.
#:
#: A callable rather than a registry import, for the reason every other seam in this estate is one: the
#: builder stays pure and testable, and a deployment with no warehouse registry supplies nothing rather
#: than a stub that lies. Wired once in `main.py`, where the app-level binding cache lives.
#:
#: IT CANNOT BE A STRING SPLIT. `project_namespace` joins with `-` ("acme", "bronze") -> "acme-bronze",
#: but `PROJECT_PATTERN` ALLOWS `-` inside a project id — so "acme-bronze" is ambiguous between project
#: `acme` and project `acme-bronze`, and guessing wrong notifies the wrong tenant's watchers. The
#: registry binding is the only sound answer.
type ProjectResolver = Callable[[str], Awaitable[str | None]]


class EmitFields(TypedDict, total=False):
    """The OPTIONAL metadata every emit accepts, declared ONCE (CAT-CORE-07).

    This block was spelled out in full on all eight emitter methods — the Protocol's two, and
    ``OriginatorBoundEmitter``'s, ``NoopEmitter``'s and ``_BaseLineageEmitter``'s pairs — and it had
    already grown twice while copied (``project`` and ``originator`` were added to every one of the
    eight in a single commit; the block went from eleven keywords to twelve).

    That is not a tidiness problem. A copy that misses a keyword does not fail to type: it silently
    DROPS the field. ``project`` is WATCH targeting's only key, so dropping it means an event nobody
    watching the tenant is told about; ``originator`` names the person a service wrote FOR, so dropping
    it puts the row in the wrong inbox. Both were live defects on this exact seam.

    PEP 692 ``Unpack``, so every caller keeps the identical keyword call syntax — nothing at any call
    site changes — while each implementation names the bundle instead of restating it.

    The REQUIRED arguments (``table_id``, ``namespace``, ``author``, ``version``, and ``operation`` on
    the write side) stay explicit: they differ between ``emit_create`` and ``emit_write``, and a
    required field in a ``total=False`` bundle would be an optional one.
    """

    #: For a create, the run id stamped into the Lance file (#21); otherwise a fresh id per write.
    run_id: str | None
    #: The caller's bearer, forwarded so lineage ingest accepts it when the lineage service has OIDC on.
    authorization: str | None
    #: The physical Lance URI → the standard ``dataSource`` facet.
    source_uri: str | None
    #: The per-version column schema → the standard ``schema`` facet (#24).
    schema_fields: SchemaFields | None
    #: The version-pinned source dataset(s) this write DERIVES FROM.
    inputs: list[InputRef] | None
    #: The run the writer named, whose id and job ride the ``lance`` facet.
    run: RunContext | None
    #: The TENANT owning the namespace — WATCH targeting's only key.
    project: str | None
    #: The person a SERVICE made this write for, when ``author`` is the service.
    originator: str | None
    #: The Lance REF the write committed to (``None`` = main). Absent for main rather than the literal
    #: "main": every historical write carries none, so a literal would make them all read as a different
    #: ref, and a consumer testing PRESENCE would answer "was this a branch write?" with yes for every one.
    branch: str | None
    #: The incarnation of ``branch`` the write committed to (pylance's ``branch_identifier``): a recreated
    #: branch restarts its version numbering, so the ref and version alone name two commits.
    branch_identifier: str | None
    #: Whether the commit added rows, which makes a create a write (:func:`build_write_event`).
    commits_rows: bool
    #: The table's and its namespace's uuids (ADR 0173 §10), which the output names beside its name.
    uuids: DatasetUuids | None


@runtime_checkable
class LineageEmitter(Protocol):
    """Emits catalog write events to the lineage service (best-effort)."""

    async def project_for(self, top_ns: str) -> str | None:
        """The tenant owning ``top_ns``, or ``None`` when it cannot be resolved."""
        ...

    async def emit_create(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int,
        **fields: Unpack[EmitFields],
    ) -> None: ...

    async def emit_write(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int | None,
        operation: str,
        **fields: Unpack[EmitFields],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class OriginatorBoundEmitter:
    """One request's ORIGINATOR claim bound to the app-scoped emitter.

    `enforce_author` overwrites `author` with the authenticating service's sub, so a write a service
    makes FOR a person can only name that person through `lance.originator`. Every layer below already
    carried the field; nothing above could set it, which made the capability unreachable from any door.

    A wrapper rather than a field on the emitter, because the two have different lifetimes: the emitter
    is built ONCE in the lifespan and shared by every concurrent request, so a claim stored on it would
    ride onto a different caller's event — a row in the wrong person's inbox, which is worse than the
    silence. Frozen and slotted so that is not merely a convention. The transport stays shared; only the
    binding is per-request.

    An explicitly passed `originator` still wins: a producer that resolved a better answer than the
    header is not overruled by the door.
    """

    inner: LineageEmitter
    originator: str | None

    def _bind(self, fields: EmitFields) -> EmitFields:
        """This request's ORIGINATOR claim, applied only where the producer supplied none.

        An explicitly passed ``originator`` still wins: a producer that resolved a better answer than
        the header is not overruled by the door.
        """
        return {**fields, "originator": fields.get("originator") or self.originator}

    async def project_for(self, top_ns: str) -> str | None:
        return await self.inner.project_for(top_ns)

    async def emit_create(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int,
        **fields: Unpack[EmitFields],
    ) -> None:
        await self.inner.emit_create(table_id=table_id, namespace=namespace, author=author, version=version, **self._bind(fields))

    async def emit_write(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int | None,
        operation: str,
        **fields: Unpack[EmitFields],
    ) -> None:
        await self.inner.emit_write(table_id=table_id, namespace=namespace, author=author, version=version, operation=operation, **self._bind(fields))


class NoopEmitter:
    """The emitter used when lineage emission is disabled — does nothing."""

    async def project_for(self, top_ns: str) -> str | None:
        return None

    async def emit_create(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int,
        **fields: Unpack[EmitFields],
    ) -> None:
        return None

    async def emit_write(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int | None,
        operation: str,
        **fields: Unpack[EmitFields],
    ) -> None:
        return None


class _BaseLineageEmitter:
    """Shared emit logic; subclasses implement only the transport (``_send``).

    ``emit_create`` is ``emit_write`` with ``operation=create_table``; ``emit_write`` builds the standard
    OpenLineage RunEvent (identical for every transport) and hands it to ``_send``. Both transports are
    best-effort — ``_send`` swallows failures so a lineage outage never breaks a catalog write."""

    _job_namespace: str
    #: Set by `make_emitter`; absent in the hand-constructed emitters the tests build, which is why it
    #: carries a class-level default rather than being required in every `__init__`.
    _project_resolver: ProjectResolver | None = None
    #: The catalog's self-description for the standard `catalog` facet ([[LIN-002]]), set the same way
    #: and for the same reason. Empty renders NO facet rather than a blank one, which is what keeps an
    #: emitter built without them honest instead of claiming an unnamed catalog.
    _catalog_impl: str = ""
    _warehouse_uri: str = ""
    #: Whether a write of an operation under this parent namespace goes unannounced: a row write in the namespace the
    #: catalog keeps for provenance (`catalog.services.provenance_namespace`), whose rows ARE the announcements lineage
    #: recorded, so announcing their own commits would record them again, without end. Set by `make_emitter`.
    _unannounced: Callable[[str, str], bool] | None = None

    async def project_for(self, top_ns: str) -> str | None:
        """Resolve the tenant, swallowing failure. BEST-EFFORT LIKE THE EMIT ITSELF: this runs on a
        committed write, and a registry blip must cost the watchers their notification, never the
        caller their request. `None` degrades to exactly the pre-existing behaviour (author only)."""
        if self._project_resolver is None or not top_ns:
            return None
        try:
            return await self._project_resolver(top_ns)
        except Exception:
            log.debug("lineage project resolution failed for %r — emitting without a tenant", top_ns, exc_info=True)
            return None

    async def emit_create(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int,
        **fields: Unpack[EmitFields],
    ) -> None:
        # The whole optional bundle rides through verbatim: a create can DERIVE FROM a version-pinned
        # source (a client's publish, a job's first write) and carry producer run facets, the
        # same metadata merge_insert has always accepted. `project`/`originator` were declared and
        # silently dropped here until 2026-08-18 — the kwarg existing per-copy is what hid it, and the
        # shared bundle is what stops it recurring.
        await self.emit_write(table_id=table_id, namespace=namespace, author=author, version=version, operation=CREATE_TABLE, **fields)

    async def emit_write(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int | None,
        operation: str,
        **fields: Unpack[EmitFields],
    ) -> None:
        # HERE, the one place every catalog emit passes through, so no door can announce a provenance row.
        if self._unannounced is not None and self._unannounced(namespace, operation):
            return
        # Resolved here rather than per call site: the mapping is mechanical, and a per-caller kwarg
        # is a rule every future endpoint eventually forgets (none ever passed one). Best-effort —
        # this runs on a committed write, so an unresolvable tenant costs a notification, not a request.
        resolved_project = fields.get("project") or await self.project_for(namespace)
        event = build_write_event(
            table_id=table_id,
            namespace=namespace,
            author=author,
            version=version,
            operation=operation,
            # For a create this is the run id stamped into the Lance file (#21); for other writes a
            # fresh id. Generate one only when the caller didn't supply it.
            run_id=fields.get("run_id") or str(uuid.uuid4()),
            event_time=datetime.now(UTC).isoformat(),
            job_namespace=self._job_namespace,
            source_uri=fields.get("source_uri"),
            schema_fields=fields.get("schema_fields"),
            inputs=fields.get("inputs"),
            run=fields.get("run"),
            project=resolved_project,
            originator=fields.get("originator"),
            branch=fields.get("branch"),
            branch_identifier=fields.get("branch_identifier"),
            commits_rows=fields.get("commits_rows", False),
            uuids=fields.get("uuids"),
            catalog_impl=self._catalog_impl,
            warehouse_uri=self._warehouse_uri,
        )
        await self._send(event, operation=operation, table_id=table_id, authorization=fields.get("authorization"))

    async def _send(self, event: dict[str, Any], *, operation: str, table_id: str, authorization: str | None) -> None:  # pragma: no cover — abstract
        raise NotImplementedError


class HttpLineageEmitter(_BaseLineageEmitter):
    """POSTs OpenLineage events to the lineage service, swallowing every failure."""

    def __init__(self, client: httpx.AsyncClient, url: str, *, job_namespace: str) -> None:
        self._client = client
        self._url = url
        self._job_namespace = job_namespace

    async def _send(self, event: dict[str, Any], *, operation: str, table_id: str, authorization: str | None) -> None:
        # Forward the caller's bearer so ingest accepts the event when the lineage service has OIDC on
        # (else the event 401s and is silently dropped). Lineage binds the author to this verified principal.
        headers = {"Authorization": authorization} if authorization else None
        try:
            response = await self._client.post(self._url, json=event, headers=headers)
            response.raise_for_status()
        except Exception as exc:
            _emit_failed.add(1, {"lance.catalog.transport": "http"})
            log.warning("lineage_emit_failed", extra={"operation": operation, "table": table_id, "error": str(exc)})


class DaprEmitter(_BaseLineageEmitter):
    """Publishes OpenLineage events to a **Dapr** ``pubsub.jetstream`` component.

    We publish to the local Dapr **sidecar** (``DaprClient.publish_event``); the sidecar persists to NATS
    JetStream and owns retry/backoff (retry exhaustion dead-letter-parks on the subscriber's ``dlq.*``
    topic — Dapr-native DLQ, default-on via the ``dapr.resiliency.enabled`` chart resiliency, park-and-alert
    not replay; docs/RESILIENCE.md gap #2, fixed 2026-07-12) + W3C trace-context propagation
    as *component config*, so the app holds no broker client (the decoupled microservice path). The topic
    is versioned (``lineage.events.v1``). ``authorization`` is unused: the topic authenticates the sidecar that
    delivered an event and not the producer that wrote it, so the ``author`` stamped inside is a claim until the
    event is signed. A catalog holding a signing key signs each authored event as itself (:meth:`_signed`), and
    where signing is enforced the subscriber admits an event only when that signature verifies against the keys
    the catalog publishes (the anti-forgery ``enforce_author`` guard is only for the open HTTP endpoint).
    """

    def __init__(
        self,
        client: DaprClient,
        pubsub: str,
        topic: str,
        *,
        job_namespace: str,
        timeout_seconds: float,
        outbox_uri: str = "",
        storage_options: Callable[[], StorageOptions] | None = None,
        signing: SigningKeyHolder[SigningKey] | None = None,
    ) -> None:
        self._client = client
        self._pubsub = pubsub
        self._topic = topic
        self._job_namespace = job_namespace
        self._timeout_seconds = timeout_seconds
        self._outbox_uri = outbox_uri
        #: Read at every stage, never held: the key pair in it rotates while the catalog runs ([[XC-082]]).
        self._storage_options = storage_options
        #: THIS CATALOG'S OWN SIGNING KEY ([[LH-064]]). None is a catalog with no store to sign from, which emits
        #: unsigned as it always did. A holder without a key emits nothing at all, never an unsigned event.
        self._signing = signing

    def _signed(self, event: dict[str, Any]) -> dict[str, Any]:
        """The event as it goes on the wire: signed by this catalog, or unchanged when it does not sign.

        THE AUTHOR DECIDES THE SHAPE. A catalog event is authored by the signed-in PERSON, and this
        service holds no credential of theirs — so it signs as ITSELF and DECLARES the subject it is
        acting for. That is an attestation ("the catalog authenticated this person and says so"), not
        a claim that the person signed; only their own credential could make the second. A RunEvent and a
        DatasetEvent are signed alike: the author rides the dataset's facets on the second shape.

        AN UNAUTHORED EVENT IS LEFT UNSIGNED, and this is the leg that would be easy to get backwards: a verifier
        refuses a signer that is not its event's author, so a signature on an event with none could never verify.
        The catalog stamps no author when it authenticated nobody, and that estate does not enforce signatures.
        The key is asked for first, so a signer without one emits nothing, whatever the event.

        Raises:
            SigningKeyUnavailableError: this catalog signs and has no key.
            CanonError: the event has no canonical form.
        """
        if self._signing is None:
            return event
        key = self._signing.key()
        author = author_of(event)
        if author is None:
            return event
        on_behalf_of = None if author == self._signing.identity else author
        return attach_signature(event, key=key, identity=self._signing.identity, on_behalf_of=on_behalf_of)

    async def _send(self, event: dict[str, Any], *, operation: str, table_id: str, authorization: str | None) -> None:
        try:
            # SIGNED INSIDE THE TRY, like everything else about an emit: this runs AFTER the Lance write committed, so a
            # signature that cannot be made is a lost announcement, counted and logged, and never a failed request.
            event = self._signed(event)
            # STAGED, then published, then dropped on ack (#4). The emit is inline-awaited and
            # best-effort AFTER the Lance write commits, so an unstaged event is lost outright to a crash
            # between the write and the publish: the data exists on storage and the feed never records
            # it. Worse than a provenance hole — a user's pipeline reads this announcement off lineage's
            # `GET /events` to start its next hop, so a lost one means the next tier's run silently never
            # happens. Staging narrows that window to commit→stage; it does not close it.
            #
            # Degrades to exactly the previous plain publish when `outbox_uri` is empty, which is the
            # default — so this is inert until a deployment sets LANCE_LINEAGE_OUTBOX_URI. Bounded by
            # the same timeout, so a hung sidecar still cannot pin the request path.
            await outbox.publish_lineage_with_outbox(
                self._client,
                outbox_uri=self._outbox_uri,
                storage_options=self._storage_options() if self._storage_options is not None else {},
                # WHICHEVER SHAPE THIS IS. A static metadata change has no run id, and staging one
                # under "" puts it beyond the relay — the exact loss the outbox exists to prevent.
                run_id=event_identity(event),
                event_json=json.dumps(event),
                pubsub_name=self._pubsub,
                topic_name=self._topic,
                timeout_seconds=self._timeout_seconds,
            )
        except (SigningKeyUnavailableError, CanonError) as exc:
            _emit_failed.add(1, {"lance.catalog.transport": "dapr"})
            log.warning("lineage_emit_withheld_unsigned", extra={"operation": operation, "table": table_id, "error": str(exc)})
        except Exception as exc:
            _emit_failed.add(1, {"lance.catalog.transport": "dapr"})
            log.warning("lineage_publish_failed", extra={"operation": operation, "table": table_id, "error": str(exc)})


def make_emitter(
    *,
    enabled: bool,
    transport: str,
    url: str | None,
    client: httpx.AsyncClient | None,
    dapr: DaprClient | None,
    pubsub: str,
    topic: str,
    job_namespace: str,
    timeout_seconds: float = 5.0,
    project_resolver: ProjectResolver | None = None,
    outbox_uri: str = "",
    storage_options: Callable[[], StorageOptions] | None = None,
    catalog_impl: str = "",
    warehouse_uri: str = "",
    signing: SigningKeyHolder[SigningKey] | None = None,
    unannounced: Callable[[str, str], bool] | None = None,
) -> LineageEmitter:
    """Select the lineage transport: ``dapr`` (durable pub/sub via the sidecar) or ``http`` (direct POST);
    no-op when disabled or unwired (a half-configured transport must never silently become the other).

    ``unannounced`` answers, for a write's parent namespace and operation, whether it goes unannounced (the provenance
    namespace's own rows)."""
    if not enabled:
        return NoopEmitter()
    if transport == "dapr" and dapr is not None:
        emitter: _BaseLineageEmitter = DaprEmitter(
            dapr,
            pubsub,
            topic,
            job_namespace=job_namespace,
            timeout_seconds=timeout_seconds,
            outbox_uri=outbox_uri,
            storage_options=storage_options,
            # ONLY THE DAPR TRANSPORT SIGNS, and that is the whole surface that needs it: the HTTP
            # emitter posts to lineage's own door, which runs `enforce_author` against a real bearer —
            # a principal the bus door does not have and the signature exists to supply.
            signing=signing,
        )
        emitter._project_resolver = project_resolver
        emitter._catalog_impl = catalog_impl
        emitter._warehouse_uri = warehouse_uri
        emitter._unannounced = unannounced
        return emitter
    if transport == "http" and url and client is not None:
        http_emitter = HttpLineageEmitter(client, url, job_namespace=job_namespace)
        http_emitter._project_resolver = project_resolver
        http_emitter._catalog_impl = catalog_impl
        http_emitter._warehouse_uri = warehouse_uri
        http_emitter._unannounced = unannounced
        return http_emitter
    return NoopEmitter()


async def emit_write_event(
    emitter: LineageEmitter,
    segments: list[str],
    *,
    author: str | None,
    version: int | None,
    operation: str,
    authorization: str | None,
    uuids: UuidReader,
    known: DatasetUuids | None = None,
    branch: str | None = None,
    branch_identifier: str | None = None,
    schema_fields: SchemaFields | None = None,
    source_uri: str | None = None,
    inputs: list[InputPin] | None = None,
    run: RunContext | None = None,
    commits_rows: bool = False,
) -> None:
    """Publish a best-effort lineage event for a catalog mutation, awaited INLINE in the handler.

    Awaited (not queued via FastAPI ``BackgroundTasks``) so the event reaches the durable Dapr/JetStream
    transport BEFORE the response returns — ``BackgroundTasks`` have no retry and die with the worker
    (fastapi anti-pattern). ``emit_write`` is best-effort (it swallows a publish failure), so awaiting it
    never fails the catalog write; JetStream message-durability + the feed's insert-only merge on its key
    give at-least-once delivery that lands one row. ``version=None`` records the run without a version.
    ``source_uri`` attaches the standard dataSource facet (the physical storage URI) — passed by ops that
    (re)attach a location, e.g. ``register``/``declare``.
    ``inputs`` names the source dataset(s) this write is DERIVED FROM, each optionally version-pinned (a
    rename passes its source; a pipeline's write passes the inputs its run names); ``run`` names that run's id and job.
    ``commits_rows`` says the commit added rows, which makes a create a write (:func:`build_write_event`).
    Names come from ``fga.canonical_object_id``, so the lineage Dataset is named as the catalog names the table.

    ``uuids`` reads the table's uuid and its namespace's, which the event names beside the names (ADR 0173 §10), and each
    input's. A door whose act ends the table read them before acting and passes them as ``known``. Otherwise the claim at
    ``source_uri``, the location the door acted on, names the table, so a rename landing since cannot lend the event
    another table's uuid; a door that names no location reads the claim of the table its name names now.
    """
    refs = await input_refs(inputs, uuids=uuids)
    table_uuids = known if known is not None else await uuids.table(segments, location=source_uri)
    # THE TENANT, resolved from the table's TOP namespace segment — the rung the warehouse registry
    # binds. Done HERE rather than at the eight call sites because not one of them has a project in
    # scope (`endpoints/data.py` does not mention the word), and because a lookup repeated eight times
    # is eight chances to derive it differently. `project_for` is best-effort and returns `None` on any
    # failure, which degrades to exactly the previous behaviour: the author is told, the watchers are not.
    project = await emitter.project_for(segments[0] if segments else "")
    await emitter.emit_write(
        table_id=fga.canonical_object_id(segments),
        namespace=fga.parent_namespace_id(segments) or "",
        author=author,
        version=version,
        operation=operation,
        run_id=str(uuid.uuid4()),
        authorization=authorization,
        source_uri=source_uri,
        schema_fields=schema_fields,
        inputs=refs,
        run=run,
        project=project,
        branch=branch,
        branch_identifier=branch_identifier,
        commits_rows=commits_rows,
        uuids=table_uuids,
    )
