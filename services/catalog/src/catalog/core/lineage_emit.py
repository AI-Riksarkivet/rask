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
  service subscribes via its
  own sidecar. The outbox gap (crash between the Lance write and publish) remains: the catalog has no
  DB for a transactional outbox; the durable producer is the Ray job (future), per microservices.md.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple, Protocol, runtime_checkable

import httpx
from common import dapr_publish, fga
from common.openlineage import (
    DATASOURCE_FACET_SCHEMA_URL,
    RUN_EVENT_SCHEMA_URL,
    VERSION_FACET_SCHEMA_URL,
    custom_facet,
    schema_facet,
)
from common.schema import SchemaFields
from dapr.aio.clients import DaprClient
from opentelemetry import metrics
from pydantic import BaseModel


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_emit_failed = _meter.create_counter(
    "catalog.lineage_emit.failed",
    unit="{event}",
    description="Best-effort catalog lineage emits that failed terminally (the catalog has no outbox — "
    "each failure is a lost event unless reconcile back-fills it), by transport.",
)

#: Operation markers carried in the OpenLineage ``lance`` run facet. The lineage service keys the
#: ``(:User)-[:CREATED]->(:Dataset)`` edge off ``create_table`` specifically, so the two sides share
#: these contract strings (see ``lineage/repository.py``); the rest just record a versioned ``WROTE``.
CREATE_TABLE = "create_table"
INSERT = "insert"
MERGE_INSERT = "merge_insert"
UPDATE = "update"
DELETE = "delete"
#: A table drop — recorded as a versionless run on the dataset (it has no version after the drop). The
#: Dataset node PERSISTS in the graph as a historical provenance record (Marquez keeps dropped datasets too);
#: the operation names it a drop so a reader can tell it was deleted, not just last-written.
DROP_TABLE = "drop_table"
#: A table deregister — detaches the table from the catalog WITHOUT deleting its data. Recorded as a
#: versionless run (asymmetric with drop, which deletes) so the detach has a provenance marker instead of
#: leaving the Dataset node looking like a still-live, never-touched table.
DEREGISTER_TABLE = "deregister_table"
#: Schema-evolution ops. Each bumps the Lance version; add/alter/drop change the column set, so their WROTE
#: edge carries the NEW per-version schema facet (the payoff: ``/datasets/{id}/schema`` + ``/columns`` follow
#: the evolution instead of freezing at create-time). ``update_field_metadata`` / ``update_schema_metadata``
#: bump the version without changing columns but still record a versioned WROTE for provenance completeness.
ADD_COLUMNS = "add_columns"
ALTER_COLUMNS = "alter_columns"
DROP_COLUMNS = "drop_columns"
UPDATE_FIELD_METADATA = "update_field_metadata"
UPDATE_SCHEMA_METADATA = "update_schema_metadata"
#: Index lifecycle. Building/dropping an index bumps the Lance version (new manifest) without touching data
#: or schema; recorded so provenance shows when a scalar/vector index was (re)built or removed.
CREATE_INDEX = "create_index"
DROP_INDEX = "drop_index"
#: Restore moves the table's current version to a prior one — a real version-state change, recorded as a
#: versioned WROTE at the new (restored) version.
RESTORE_TABLE = "restore_table"
#: Declare reserves a table id with no data yet (versionless); register attaches an existing storage
#: location. Both are "the table came into existence in this catalog" events, so — like ``create_table`` —
#: they key a ``(:User)-[:CREATED]->(:Dataset)`` edge (see ``lineage/repository.py`` ``_CREATE_OPS``).
DECLARE_TABLE = "declare_table"
REGISTER_TABLE = "register_table"
#: #17 model promotion: the ``blessed`` tag on a model registry (``models$<model>``) was moved to a candidate
#: Lance version (candidate→blessed). A metadata-only tag move mints no new data version, so the emitted
#: ``version`` is the tag's TARGET (the promoted model version), not a fresh write — a distinct op so a
#: blessing is never mistaken for a training run or a data write on the run board.
PROMOTE_MODEL = "promote_model"

#: OpenLineage ``producer`` URI — identifies the software that emitted the event (spec-required,
#: and what a Marquez-style consumer records as the event source).
_PRODUCER = "https://github.com/Borg93/lance-ns/tree/main/services/catalog/core/lineage_emit.py"

#: OpenLineage standard ``DatasetVersionDatasetFacet`` schema URL. The output dataset carries this
#: facet so the lineage service records the Lance version on the ``WROTE`` edge
#: (``repository.output_version`` reads ``outputs[].facets.version.datasetVersion``) — without it a real
#: ``create_table`` persists a versionless edge (the custom ``lance`` run facet is not read for version).
_VERSION_FACET_SCHEMA = VERSION_FACET_SCHEMA_URL

#: OpenLineage standard ``DatasourceDatasetFacet`` schema URL. The output dataset carries this facet with
#: the **physical storage URI** so the lineage service can find the real Lance file on object storage and
#: cross-check the on-disk version (#23 reconcile — ``lineage.models.Dataset.source_uri`` reads
#: ``facets.dataSource.uri``). Without it, reconcile has no URI to read → every real table looks
#: ``missing_on_storage`` (the moat was broken).
_DATASOURCE_FACET_SCHEMA = DATASOURCE_FACET_SCHEMA_URL


class InputPin(BaseModel):
    """A source dataset an emitted write derives from, with the EXACT version it consumed (or ``None``).

    The API-surface shape (catalog ``segments``, as any ``{id}`` route uses); ``emit_write_event`` resolves
    it to the canonical lineage/FGA id. A pinned ``version`` becomes a ``DatasetVersionDatasetFacet`` on the
    input edge — the reproducibility handshake a derived write (a mover's merge_insert from ``source@N``)
    needs so its provenance names the precise input version, not just the dataset.
    """

    segments: list[str]
    version: int | None = None


class InputRef(NamedTuple):
    """A resolved input edge for the wire builder: canonical ``(namespace, name)`` + pinned version."""

    namespace: str
    name: str
    version: int | None


def _input_dataset(ref: InputRef) -> dict[str, Any]:
    """An OpenLineage INPUT dataset, carrying the standard ``DatasetVersionDatasetFacet`` when a source
    version is pinned."""
    dataset: dict[str, Any] = {"namespace": ref.namespace, "name": ref.name}
    if ref.version is not None:
        dataset["facets"] = {
            "version": {
                "_producer": _PRODUCER,
                "_schemaURL": _VERSION_FACET_SCHEMA,
                "datasetVersion": str(ref.version),
            }
        }
    return dataset


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
    extra_run_facets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the OpenLineage ``RunEvent`` (wire JSON) for any catalog write to a table.

    ``table_id`` is the catalog's canonical id (e.g. ``alpha$bronze$images``) so the lineage
    ``Dataset`` name matches the OpenFGA object id byte-for-byte — one identity across the three
    governance axes. ``operation`` is the catalog op (``create_table`` / ``insert`` / ``merge_insert``
    / ``update`` / ``delete``). ``version`` is the Lance version the write produced; when it is ``None``
    (e.g. an insert whose response carries no version) the standard version facet is omitted so the
    ``WROTE`` edge records the run without asserting a version. ``run_id`` / ``event_time`` are injected
    so the builder is pure and deterministically testable.

    ``inputs`` names the ``(namespace, table_id)`` datasets this write is DERIVED FROM — a rename passes the
    SOURCE table so the destination's provenance chain is not severed (the graph shows dest←source instead
    of the renamed table appearing as an orphan with no history). Default ``None`` → no input edge, the
    normal case for a fresh write.
    """
    lance_fields: dict[str, Any] = {"operation": operation}
    if version is not None:
        lance_fields["version"] = version
    # Caller-supplied run facets FIRST (e.g. a `params` training-params facet on a merge), already spec-shaped
    # by shape_run_facets, so the catalog stays un-opinionated about their content. The catalog-OWNED `lance`
    # (operation/version) and `author` facets are stamped AFTER — they always win a name collision, so a
    # producer can never forge the operation (a false CREATED edge) or the author on the trusted transport,
    # even if a reserved name slipped past shape_run_facets' denylist (defense in depth).
    run_facets: dict[str, Any] = dict(extra_run_facets) if extra_run_facets else {}
    run_facets["lance"] = custom_facet(_PRODUCER, **lance_fields)
    if author is not None:
        run_facets["author"] = custom_facet(_PRODUCER, name=author, sub=author)
    output: dict[str, Any] = {"namespace": namespace, "name": table_id}
    facets: dict[str, Any] = {}
    if version is not None:
        # Standard version facet → the lineage WROTE edge carries the Lance version (#20).
        facets["version"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _VERSION_FACET_SCHEMA,
            "datasetVersion": str(version),
        }
    if source_uri:
        # Standard dataSource facet → the physical Lance URI, so #23 reconcile can read the on-disk file.
        facets["dataSource"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _DATASOURCE_FACET_SCHEMA,
            "name": source_uri,
            "uri": source_uri,
        }
    if schema_fields:
        # Standard schema facet → the per-version column schema (blob/vector-aware) on the WROTE edge (#24),
        # so a catalog-written table has real columns in the graph, not empty until a compute job re-asserts.
        # Built by the SHARED common.openlineage helper — one spec version across all emitters.
        facets["schema"] = schema_facet(_PRODUCER, schema_fields)
    if facets:
        output["facets"] = facets
    return {
        "eventType": "COMPLETE",
        "eventTime": event_time,
        "producer": _PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA_URL,
        "run": {"runId": run_id, "facets": run_facets},
        # Job identity is per-TABLE (``<operation>.<table_id>``), not the bare op — else every table's
        # writes lump into one Job node (``insert``), which the /jobs governance fold then makes visible
        # to anyone who can see ANY of those tables. Per-table keeps the Job's output set — its access
        # handle — scoped to the one table it wrote.
        "job": {"namespace": job_namespace, "name": f"{operation}.{table_id}"},
        "inputs": [_input_dataset(ref) for ref in (inputs or [])],
        "outputs": [output],
    }


#: Run-facet names a producer may NOT set through the passthrough: the ones the catalog STAMPS itself
#: (``lance`` = operation/version, ``author`` = the verified principal) and the ones the lineage consumer
#: TRUSTS off the wire (``errorMessage`` / ``progress`` run state, ``parent`` run hierarchy). Left open, a
#: caller-named ``author``/``lance`` facet would forge the principal or the operation — minting a false
#: ``(:User)-[:CREATED]->(:Dataset)`` edge — on the Dapr transport, which trusts the catalog's stamp
#: wholesale (there is no ``enforce_author`` on that internal channel). Rejected here as a clean 4xx.
_RESERVED_RUN_FACETS = frozenset({"lance", "author", "errorMessage", "progress", "parent"})


def shape_run_facets(raw: dict[str, Any]) -> dict[str, Any]:
    """Wrap caller-supplied run-facet payloads with the spec-required ``_producer`` / ``_schemaURL``.

    The catalog stays un-opinionated about a producer's run metadata (e.g. a mover's merge carrying
    training ``params``): it does not interpret the payload, it only stamps each named facet spec-legal
    via :func:`custom_facet` so a strict OpenLineage consumer accepts the event. ``raw`` maps a facet
    name to its payload object. Guarded fail-closed: the facet NAME may not be a reserved catalog-owned /
    consumer-trusted facet (:data:`_RESERVED_RUN_FACETS`) nor empty; the payload must be a JSON object; and
    its keys may not shadow the spec's ``_``-prefixed fields nor ``producer`` (all of which
    :func:`custom_facet` owns — ``producer`` is its positional arg, so a ``producer`` key would raise a bare
    ``TypeError``). Raises ``ValueError`` on any violation so the caller boundary can translate it to a 4xx.
    """
    shaped: dict[str, Any] = {}
    for name, payload in raw.items():
        if not name or name in _RESERVED_RUN_FACETS:
            raise ValueError(f"run facet {name!r} is reserved and cannot be set by a producer")
        if not isinstance(payload, dict):
            raise ValueError(f"run facet {name!r} must be a JSON object")
        if any(key.startswith("_") or key == "producer" for key in payload):
            raise ValueError(f"run facet {name!r} may not set a reserved field (_-prefixed or 'producer')")
        shaped[name] = custom_facet(_PRODUCER, **payload)
    return shaped


@runtime_checkable
class LineageEmitter(Protocol):
    """Emits catalog write events to the lineage service (best-effort)."""

    async def emit_create(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int,
        run_id: str | None = None,
        authorization: str | None = None,
        source_uri: str | None = None,
        schema_fields: SchemaFields | None = None,
    ) -> None: ...

    async def emit_write(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int | None,
        operation: str,
        run_id: str | None = None,
        authorization: str | None = None,
        source_uri: str | None = None,
        schema_fields: SchemaFields | None = None,
        inputs: list[InputRef] | None = None,
        extra_run_facets: dict[str, Any] | None = None,
    ) -> None: ...


class NoopEmitter:
    """The emitter used when lineage emission is disabled — does nothing."""

    async def emit_create(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int,
        run_id: str | None = None,
        authorization: str | None = None,
        source_uri: str | None = None,
        schema_fields: SchemaFields | None = None,
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
        run_id: str | None = None,
        authorization: str | None = None,
        source_uri: str | None = None,
        schema_fields: SchemaFields | None = None,
        inputs: list[InputRef] | None = None,
        extra_run_facets: dict[str, Any] | None = None,
    ) -> None:
        return None


class _BaseLineageEmitter:
    """Shared emit logic; subclasses implement only the transport (``_send``).

    ``emit_create`` is ``emit_write`` with ``operation=create_table``; ``emit_write`` builds the standard
    OpenLineage RunEvent (identical for every transport) and hands it to ``_send``. Both transports are
    best-effort — ``_send`` swallows failures so a lineage outage never breaks a catalog write."""

    _job_namespace: str

    async def emit_create(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int,
        run_id: str | None = None,
        authorization: str | None = None,
        source_uri: str | None = None,
        schema_fields: SchemaFields | None = None,
    ) -> None:
        await self.emit_write(
            table_id=table_id,
            namespace=namespace,
            author=author,
            version=version,
            operation=CREATE_TABLE,
            run_id=run_id,
            authorization=authorization,
            source_uri=source_uri,
            schema_fields=schema_fields,
        )

    async def emit_write(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int | None,
        operation: str,
        run_id: str | None = None,
        authorization: str | None = None,
        source_uri: str | None = None,
        schema_fields: SchemaFields | None = None,
        inputs: list[InputRef] | None = None,
        extra_run_facets: dict[str, Any] | None = None,
    ) -> None:
        event = build_write_event(
            table_id=table_id,
            namespace=namespace,
            author=author,
            version=version,
            operation=operation,
            # For a create this is the run id stamped into the Lance file (#21); for other writes a
            # fresh id. Generate one only when the caller didn't supply it.
            run_id=run_id or str(uuid.uuid4()),
            event_time=datetime.now(UTC).isoformat(),
            job_namespace=self._job_namespace,
            source_uri=source_uri,
            schema_fields=schema_fields,
            inputs=inputs,
            extra_run_facets=extra_run_facets,
        )
        await self._send(event, operation=operation, table_id=table_id, authorization=authorization)

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
    is versioned (``lineage.events.v1``). ``authorization`` is unused — the pub/sub topic is an internal
    catalog-only channel, so the subscriber trusts the verified ``author`` the catalog stamped (the
    anti-forgery ``enforce_author`` guard is only for the open HTTP endpoint).
    """

    def __init__(self, client: DaprClient, pubsub: str, topic: str, *, job_namespace: str, timeout_seconds: float) -> None:
        self._client = client
        self._pubsub = pubsub
        self._topic = topic
        self._job_namespace = job_namespace
        self._timeout_seconds = timeout_seconds

    async def _send(self, event: dict[str, Any], *, operation: str, table_id: str, authorization: str | None) -> None:
        try:
            # Bounded so a hung sidecar can't pin the inline-awaited emit on the create/write request path.
            await dapr_publish.publish_event(
                self._client,
                timeout_seconds=self._timeout_seconds,
                pubsub_name=self._pubsub,
                topic_name=self._topic,
                data=json.dumps(event),
                data_content_type="application/json",
            )
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
) -> LineageEmitter:
    """Select the lineage transport: ``dapr`` (durable pub/sub via the sidecar) or ``http`` (direct POST);
    no-op when disabled or unwired (a half-configured transport must never silently become the other)."""
    if not enabled:
        return NoopEmitter()
    if transport == "dapr" and dapr is not None:
        return DaprEmitter(dapr, pubsub, topic, job_namespace=job_namespace, timeout_seconds=timeout_seconds)
    if transport == "http" and url and client is not None:
        return HttpLineageEmitter(client, url, job_namespace=job_namespace)
    return NoopEmitter()


async def emit_write_event(
    emitter: LineageEmitter,
    segments: list[str],
    *,
    delimiter: str,
    author: str | None,
    version: int | None,
    operation: str,
    authorization: str | None,
    schema_fields: SchemaFields | None = None,
    source_uri: str | None = None,
    inputs: list[InputPin] | None = None,
    extra_run_facets: dict[str, Any] | None = None,
) -> None:
    """Publish a best-effort lineage ``WROTE`` event for a catalog mutation, awaited INLINE in the handler.

    Awaited (not queued via FastAPI ``BackgroundTasks``) so the event reaches the durable Dapr/JetStream
    transport BEFORE the response returns — ``BackgroundTasks`` have no retry and die with the worker
    (fastapi anti-pattern). ``emit_write`` is best-effort (it swallows a publish failure), so awaiting it
    never fails the catalog write; JetStream message-durability + the lineage consumer's idempotent
    MERGE-on-``run_id`` give the at-least-once delivery. ``version=None`` records the run without a version.
    ``source_uri`` attaches the standard dataSource facet (the physical storage URI) so #23 reconcile can
    find the on-disk file — passed by ops that (re)attach a location, e.g. ``register``/``declare``.
    ``inputs`` names the source dataset(s) this write is DERIVED FROM, each optionally version-pinned (a
    rename passes its source; a mover's merge passes ``source@N``); ``extra_run_facets`` rides caller-supplied
    run facets (e.g. training params). Ids come from ``fga`` so the lineage Dataset == the OpenFGA object.
    """
    refs = [
        InputRef(
            fga.parent_namespace_id(pin.segments, delimiter=delimiter) or "",
            fga.canonical_object_id(pin.segments, delimiter=delimiter),
            pin.version,
        )
        for pin in (inputs or [])
    ]
    await emitter.emit_write(
        table_id=fga.canonical_object_id(segments, delimiter=delimiter),
        namespace=fga.parent_namespace_id(segments, delimiter=delimiter) or "",
        author=author,
        version=version,
        operation=operation,
        run_id=str(uuid.uuid4()),
        authorization=authorization,
        source_uri=source_uri,
        schema_fields=schema_fields,
        inputs=refs or None,
        extra_run_facets=extra_run_facets,
    )
