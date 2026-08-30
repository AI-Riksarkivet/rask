"""Best-effort OpenLineage emission from the maintenance service to the lineage graph (#7b).

A maintenance sweep compacts small fragments + GCs old versions of each Lance dataset; when
``lineage_emit_enabled`` is on it records each *materially-compacted* dataset as an OpenLineage
``RunEvent`` (output = the dataset, ``operation=compaction``) — and each ``maintain:``-FAILED dataset
as a ``FAIL`` event with the standard ``errorMessage`` facet (§4 failure visibility; deterministic
per-dataset run id as the flood guard) — published to the **Dapr** ``pubsub.jetstream`` component —
the SAME pubsub component + topic the catalog publishes to and the lineage service subscribes to, so
compaction runs (and failures) show up in ``producers()`` alongside the writes.
The sidecar owns retry/backoff + trace-propagation (no DLQ — see docs/RESILIENCE.md gap #2) as component
config (no broker client here).

Self-contained: the maintenance service never imports the catalog (zero cross-service imports — the
mergeability invariant). The only shared code is ``service_kit.governed.fga`` for the id↔namespace derivation, so the
lineage ``Dataset`` name == the OpenFGA object id == the catalog table id across all three governance axes.

Two deliberate choices:

* **Versionless.** A compaction is *maintenance*, not a data write, so the event asserts no data version —
  the lineage repository records a ``(:Run)-[:WROTE]->(:Dataset)`` with no version and (no inputs →) no
  ``DERIVED_FROM``. This also keeps it out of ``reconcile``'s ``latest_write_version`` (which must track
  real writes), so a compaction never makes the graph look "ahead" of the last data write.
* **Best-effort.** A sidecar/broker outage logs + drops; auditing must never fail a maintenance sweep.

This layer is **pure** (builders + emitter transports only); the per-sweep orchestration that needs the
:class:`~maintenance.services.optimize.DatasetResult` type lives in ``maintenance.services.sweep`` so ``core``
never imports up into ``services``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from dapr.aio.clients import DaprClient

from service_kit.lakehouse import outbox
from service_kit.openlineage import ERROR_MESSAGE_FACET_SCHEMA_URL, RUN_EVENT_SCHEMA_URL, custom_facet, run_id_for


log = logging.getLogger(__name__)

#: OpenLineage ``lance`` run-facet operation marker for a maintenance pass. Distinct from the catalog's
#: write operations (``create_table`` / ``insert`` / ...); the lineage repository records it as a
#: versionless ``WROTE`` run on the dataset (no ``DERIVED_FROM`` — a compaction has no inputs — and, since
#: it is not ``create_table``, no ``CREATED`` edge).
COMPACTION = "compaction"

#: OpenLineage ``producer`` URI — identifies the software that emitted the event (spec-required).
_PRODUCER = "https://github.com/AI-Riksarkivet/rask/tree/main/services/maintenance/src/maintenance/core/lineage_emit.py"

#: Standard ``ErrorMessageRunFacet`` schema URL — the SAME one the medallion FAIL emitter stamps
#: (``medallion/schemas/events.py``), so both failure surfaces are one spec version.
_ERROR_FACET_SCHEMA_URL = ERROR_MESSAGE_FACET_SCHEMA_URL

#: Exception strings embed full s3:// URIs and Rust backtraces — cap the facet message so a failing
#: dataset can't turn every FAIL event into a multi-KB payload (events must stay small JSON).
_ERROR_MESSAGE_MAX_CHARS = 1000


def classify_retryable(error: str) -> bool | None:
    """BEST-EFFORT retryable classification from the error text — there is no ground-truth signal.

    pylance 8.0.0 raises no typed conflict exception (``lance.commit.CommitConflictError`` exists but is
    never raised), and Rebasable conflicts are auto-retried inside the commit layer so they never reach
    Python. Per the three-way taxonomy (``lance_docs/file_format.md`` ~5253) the string heuristic maps
    Retryable (a re-run may succeed) vs Incompatible (it won't); ``None`` when the text says neither.
    NEGATED forms are checked FIRST — real object_store/Lance messages say things like "not retryable"
    or "retry limit exceeded", and a bare substring match would stamp exactly those terminal errors
    ``retryable=true`` (review 2026-07-10).
    """
    lowered = error.lower()
    if "incompatible" in lowered:
        return False
    if "non-retryable" in lowered or "not retryable" in lowered or "retries exhausted" in lowered or "retry limit" in lowered:
        return False
    if "conflict" in lowered or "retry" in lowered:
        return True
    return None


def build_maintenance_fail_event(*, table_id: str, namespace: str, job_namespace: str, run_id: str, event_time: str, error: str) -> dict[str, Any]:
    """The FAIL twin of :func:`build_maintenance_event` — a per-dataset maintenance FAILURE (§4).

    Same job identity and the same BARE output (name only — the lineage repo then makes the ``WROTE``
    edge so ``producers()``/``/runs`` surface the failing maintenance next to the writes) but
    ``eventType=FAIL`` and the standard ``errorMessage`` run facet carrying why. NO version / schema /
    statistics facets and no inputs — a failed maintenance pass must never fabricate lineage. The
    best-effort ``retryable`` classification rides the facet as a custom extra field (the lineage model
    parses facets extra-tolerantly) only when the heuristic is confident.
    """
    error_facet: dict[str, Any] = {
        "_producer": _PRODUCER,
        "_schemaURL": _ERROR_FACET_SCHEMA_URL,
        "message": error[:_ERROR_MESSAGE_MAX_CHARS],
        "programmingLanguage": "PYTHON",
    }
    retryable = classify_retryable(error)
    if retryable is not None:
        error_facet["retryable"] = retryable
    # Derive from the COMPLETE builder rather than duplicating the envelope: the two events MUST share the
    # job identity + bare-output key or a dataset's failures and successes split across different Job /
    # Dataset nodes in the graph — building on one core makes that drift impossible.
    event = build_maintenance_event(
        table_id=table_id,
        namespace=namespace,
        job_namespace=job_namespace,
        run_id=run_id,
        event_time=event_time,
    )
    event["eventType"] = "FAIL"
    event["run"]["facets"]["errorMessage"] = error_facet
    return event


#: Schema-metadata key a PRODUCER may stamp to declare its dataset's canonical lineage name.
#:
#: The derivation below cannot name the medallion cascade's own datasets, and that is not a parsing
#: shortfall — the mapping does not exist. The chart composes those URIs from the namespace alone
#: (`.../medallion/<fromNamespace>`) while the canonical id is a separate literal (`bronze$events`,
#: `gold$htr`), so `medallion/bronze` is BOTH `bronze$events` and `bronze$pages`. No cleverer split can
#: recover a name the path never carried.
#:
#: The name must equal the OpenFGA object id: delivery re-checks `can_get_metadata` against
#: `table:<output name>`, so a wrong name counts every recipient HIDDEN — worse than emitting nothing.
#: Hence a DECLARED key rather than a guess.
#:
#: Reading it here is deliberately landed ahead of any producer writing it: the read is inert until a
#: dataset carries the key, and it makes the producer side a one-line stamp rather than a coordinated
#: change. Datasets already on disk carry no key and keep the URI derivation — the backfill gap is real
#: and is why this is additive rather than a replacement.
LINEAGE_DATASET_ID_KEY = "lineage.dataset_id"


def declared_table_id(dataset: Any) -> str | None:  # noqa: ANN401 — LanceDataset, no protocol
    """The canonical lineage name a producer stamped on the dataset, or ``None``.

    Never raises: an unreadable schema must cost this dataset its provenance, not the sweep its tick.
    """
    try:
        metadata = dict(getattr(dataset.schema, "metadata", None) or {})
    except Exception:  # noqa: BLE001 — a malformed schema is a missing name, never a failed sweep
        return None
    for key, value in metadata.items():
        name = key.decode() if isinstance(key, bytes) else str(key)
        if name == LINEAGE_DATASET_ID_KEY:
            declared = value.decode() if isinstance(value, bytes) else str(value)
            return declared or None
    return None


def table_id_from_uri(uri: str) -> str | None:
    """Extract the catalog table id from a dataset URI laid out as ``<uuid>_<table_id>``.

    The catalog lays each table out as ``s3://<bucket>/<uuid>_<table_id>/`` (see
    :func:`~maintenance.services.optimize.discover_datasets`), and ``table_id`` is the canonical lineage
    ``Dataset`` name == the OpenFGA object id. Splits the last path segment on its **first** ``_`` so a
    table id that itself contains ``_`` survives intact. Returns ``None`` when the segment has no ``_`` (not
    a catalog-laid-out table) so a stray directory never produces a bogus maintenance event.
    """
    name = uri.rstrip("/").split("/")[-1]
    _uuid, sep, table_id = name.partition("_")
    return table_id if sep and table_id else None


def build_maintenance_event(*, table_id: str, namespace: str, job_namespace: str, run_id: str, event_time: str) -> dict[str, Any]:
    """Build the OpenLineage ``RunEvent`` (wire JSON) for one dataset's compaction/GC pass.

    Versionless and input-less: a maintenance pass produces no new logical data and derives from nothing,
    so the lineage repository records a ``(:Run)-[:WROTE]->(:Dataset)`` with no version and no
    ``DERIVED_FROM`` — ``producers()`` then surfaces the compaction run next to the data writes. ``run_id`` /
    ``event_time`` are injected so the builder is pure and deterministically testable.
    """
    return {
        "eventType": "COMPLETE",
        "eventTime": event_time,
        "producer": _PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA_URL,
        "run": {"runId": run_id, "facets": {"lance": custom_facet(_PRODUCER, operation=COMPACTION)}},
        # Per-table job identity (like the catalog write emitter) — else every dataset's compaction lumps
        # into one ``compaction`` Job node whose output set spans the whole lakehouse.
        "job": {"namespace": job_namespace, "name": f"{COMPACTION}.{table_id}"},
        "inputs": [],
        "outputs": [{"namespace": namespace, "name": table_id}],
    }


@runtime_checkable
class MaintenanceEmitter(Protocol):
    """Emits compaction maintenance events (success + failure) to the lineage service (best-effort)."""

    async def emit_maintenance(self, *, table_id: str, namespace: str) -> None: ...

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str) -> None: ...


class NoopEmitter:
    """The emitter used when lineage emission is disabled (or unwired) — does nothing."""

    async def emit_maintenance(self, *, table_id: str, namespace: str) -> None:
        return None

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str) -> None:
        return None


class DaprMaintenanceEmitter:
    """Publishes maintenance events to a **Dapr** ``pubsub.jetstream`` component (the production path).

    Publishes to the local Dapr sidecar (``DaprClient.publish_event``); the sidecar persists to NATS
    JetStream and owns retry/backoff (no DLQ — docs/RESILIENCE.md gap #2) + W3C trace-context propagation
    as component config, so the app
    holds no broker client. Best-effort: a sidecar/broker outage logs + drops rather than failing the sweep.
    """

    def __init__(
        self,
        client: DaprClient,
        *,
        pubsub: str,
        topic: str,
        job_namespace: str,
        timeout_seconds: float,
        outbox_uri: str = "",
        storage_options: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._pubsub = pubsub
        self._topic = topic
        self._job_namespace = job_namespace
        self._timeout_seconds = timeout_seconds
        self._outbox_uri = outbox_uri
        self._storage_options = storage_options or {}

    async def emit_maintenance(self, *, table_id: str, namespace: str) -> None:
        # uuid4 ON PURPOSE: each materially-compacting tick is a distinct successful run (§4 decided —
        # do NOT make COMPLETE deterministic; only the FAIL path below needs the flood guard).
        event = build_maintenance_event(
            table_id=table_id,
            namespace=namespace,
            job_namespace=self._job_namespace,
            run_id=str(uuid.uuid4()),
            event_time=datetime.now(UTC).isoformat(),
        )
        await self._publish(event, table_id)

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str) -> None:
        # DETERMINISTIC run id — the flood guard: the cron re-sweeps every ~2 min, so a persistently
        # failing dataset would otherwise mint a fresh never-pruned (:Run) node per tick. One id per
        # dataset → every tick MERGEs onto ONE node in AGE, and the /events partial-unique
        # (run_id, event_type) index dedups the redelivered FAIL rows. Accepted consequences (§4 +
        # 2026-07-10 review): (a) after recovery the FAIL node stays until the Run-retention prune
        # (LINEAGE_RUN_RETENTION_DAYS — enable it alongside lineageEmit) sweeps it; (b) across DISTINCT
        # failure episodes the split is: the (:Run) node is last-wins (/runs shows the LATEST error),
        # while /events keeps the FIRST FAIL row (its keep-first terminal contract) — /runs is the live
        # view, /events the first-observation log.
        event = build_maintenance_fail_event(
            table_id=table_id,
            namespace=namespace,
            job_namespace=self._job_namespace,
            run_id=run_id_for(f"compaction-fail-{table_id}"),
            event_time=datetime.now(UTC).isoformat(),
            error=error,
        )
        await self._publish(event, table_id)

    async def _publish(self, event: dict[str, Any], table_id: str) -> None:
        try:
            # STAGED, then published, then dropped on ack (#4) — the twin of the catalog's emit. A
            # sweep's lineage event describes a committed write, so losing one leaves the graph
            # under-reporting work that really happened. Degrades to exactly the previous plain publish
            # when `outbox_uri` is unset, which is the default, and stays bounded either way so a hung
            # sidecar cannot stall the sweep.
            await outbox.publish_lineage_with_outbox(
                self._client,
                outbox_uri=self._outbox_uri,
                storage_options=self._storage_options,
                run_id=str((event.get("run") or {}).get("runId") or ""),
                event_json=json.dumps(event),
                pubsub_name=self._pubsub,
                topic_name=self._topic,
                timeout_seconds=self._timeout_seconds,
            )
        except Exception as exc:
            log.warning("maintenance_publish_failed", extra={"table": table_id, "error": str(exc)})


def make_emitter(
    *,
    enabled: bool,
    dapr: DaprClient | None,
    outbox_uri: str = "",
    storage_options: dict[str, str] | None = None,
    pubsub: str,
    topic: str,
    job_namespace: str,
    timeout_seconds: float = 5.0,
) -> MaintenanceEmitter:
    """Select the emitter: a Dapr pub/sub publisher when enabled + wired, else a no-op (never silently
    publish nowhere — a half-configured transport stays a no-op rather than pretending to emit)."""
    if enabled and dapr is not None:
        return DaprMaintenanceEmitter(
            dapr,
            pubsub=pubsub,
            topic=topic,
            job_namespace=job_namespace,
            timeout_seconds=timeout_seconds,
            outbox_uri=outbox_uri,
            storage_options=storage_options,
        )
    return NoopEmitter()
