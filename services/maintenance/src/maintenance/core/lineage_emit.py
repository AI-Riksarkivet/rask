"""Best-effort OpenLineage emission from the maintenance service to lineage's commit feed (#7b).

A maintenance sweep expires each Lance dataset's superseded versions; when ``lineage_emit_enabled`` is on it records
each dataset whose versions it reclaimed as an OpenLineage ``RunEvent`` (output = the dataset,
``operation=expire_versions``) — and each ``maintain:``-FAILED dataset as a ``FAIL`` event with the standard
``errorMessage`` facet (§4 failure visibility; deterministic per-dataset run id as the flood guard) — published to the
**Dapr** ``pubsub.jetstream`` component, the SAME pubsub component + topic the catalog publishes to and the lineage
service subscribes to, so expiry runs (and failures) land in the feed alongside the writes.
The sidecar owns retry/backoff + trace-propagation (no DLQ — see docs/RESILIENCE.md gap #2) as component
config (no broker client here).

Self-contained: the maintenance service never imports the catalog (zero cross-service imports — the
mergeability invariant). The only shared code is ``service_kit.governed.fga`` for the id↔namespace derivation, so the
lineage ``Dataset`` is named as the catalog names the table, and the event carries the table's uuid, which its OpenFGA
object names (ADR 0173 §8, §10).

Two deliberate choices:

* **Versionless.** Expiry is *maintenance*, not a data write, so the event asserts no data version, and with no
  inputs it asserts no edge (``lineage.services.provenance.edges_of``): the feed records the run, and the edge list
  stays the record of what derives from what.
* **Best-effort.** A sidecar/broker outage logs + drops; auditing must never fail a maintenance sweep.

This layer is **pure** (builders + emitter transports only); the per-sweep orchestration that needs the
:class:`~maintenance.services.expiry.DatasetResult` type lives in ``maintenance.services.sweep`` so ``core``
never imports up into ``services``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final, Protocol, runtime_checkable

from dapr.aio.clients import DaprClient

from lineage_kit import CanonError, SigningKey, attach_signature
from service_kit.governed.signing_key import SigningKeyHolder, SigningKeyUnavailableError
from service_kit.lakehouse import outbox
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.openlineage import (
    AUTHOR_FACET,
    ERROR_MESSAGE_FACET_SCHEMA_URL,
    LANCE_FACET,
    RUN_EVENT_SCHEMA_URL,
    DatasetUuids,
    author_facet,
    lance_facet,
    run_id_for,
    with_dataset_uuids,
)


log = logging.getLogger(__name__)

#: OpenLineage ``lance`` run-facet operation marker for a maintenance pass: the versions it reclaimed. Distinct from the
#: catalog's write operations (``create_table`` / ``insert`` / ...); the feed records it as a versionless run on the
#: dataset, with no edge, since expiry has no inputs. It names what the run did: maintenance compacts nothing (ADR 0155).
EXPIRE_VERSIONS = "expire_versions"

#: OpenLineage ``producer`` URI — identifies the software that emitted the event (spec-required).
_PRODUCER = "https://github.com/AI-Riksarkivet/rask/tree/main/services/maintenance/src/maintenance/core/lineage_emit.py"

#: Standard ``ErrorMessageRunFacet`` schema URL — the SAME one the medallion FAIL emitter stamps
#: (``medallion/schemas/events.py``), so both failure surfaces are one spec version.
_ERROR_FACET_SCHEMA_URL = ERROR_MESSAGE_FACET_SCHEMA_URL

#: Exception strings embed full s3:// URIs and Rust backtraces — cap the facet message so a failing
#: dataset can't turn every FAIL event into a multi-KB payload (events must stay small JSON).
_ERROR_MESSAGE_MAX_CHARS = 1000

#: The uuids of a dataset whose table and namespace were not resolved: its output names them by name alone.
UNRESOLVED: Final = DatasetUuids()


def classify_retryable(error: str) -> bool | None:
    """BEST-EFFORT retryable classification from the error text — there is no ground-truth signal.

    pylance 8.0.0 raises no typed conflict exception (``lance.commit.CommitConflictError`` exists but is
    never raised), and Rebasable conflicts are auto-retried inside the commit layer so they never reach
    Python. Per the three-way taxonomy (``lance_docs/lance/format/table/transaction.md`` ~613) the string heuristic maps
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


def build_maintenance_fail_event(
    *,
    table_id: str,
    namespace: str,
    job_namespace: str,
    run_id: str,
    event_time: str,
    error: str,
    author: str = "",
    uuids: DatasetUuids = UNRESOLVED,
) -> dict[str, Any]:
    """The FAIL twin of :func:`build_maintenance_event` — a per-dataset maintenance FAILURE (§4).

    Same job identity and the same BARE output (name only — so the feed holds the failing maintenance next to
    the writes on the same dataset) but
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
    # job identity + bare-output key or a dataset's failures and successes split across different jobs and
    # datasets in the feed and in a forwarded graph backend — building on one core makes that drift impossible.
    event = build_maintenance_event(
        table_id=table_id,
        namespace=namespace,
        job_namespace=job_namespace,
        run_id=run_id,
        event_time=event_time,
        # A FAILURE especially must be attributable: an unattributed failure is the one a person is
        # most likely to have to chase, and the COMPLETE twin carrying a signature while the FAIL did
        # not would be the more misleading of the two states.
        author=author,
        uuids=uuids,
    )
    event["eventType"] = "FAIL"
    event["run"]["facets"]["errorMessage"] = error_facet
    return event


def build_maintenance_event(
    *,
    table_id: str,
    namespace: str,
    job_namespace: str,
    run_id: str,
    event_time: str,
    author: str = "",
    uuids: DatasetUuids = UNRESOLVED,
) -> dict[str, Any]:
    """Build the OpenLineage ``RunEvent`` (wire JSON) for one dataset's expiry pass.

    Input-less and versionless: a maintenance pass produces no new logical data and derives from nothing, so the
    feed records the run next to the data writes and the edge list gains no edge. ``run_id`` / ``event_time`` are injected so the builder is pure and
    deterministically testable.

    ``author`` IS THE SERVICE'S OWN IDENTITY, NEVER A PERSON'S. A maintenance pass is nobody's request;
    signing it as the service is the only claim this emitter is entitled to make, and it is the claim
    the lineage bus door can later authorize against. Measured on the deployed lineage store 2026-09-08: 664 of
    5 644 runs carried no author, 518 of them from here — so lineage could not say who maintained a
    dataset while the CATALOG, which this same service calls through its own service door, could.

    EMPTY MEANS ABSENT, and that asymmetry is deliberate. An unconfigured identity emits NO author facet
    rather than a placeholder: `author_sub_from_payload` exists because a producer-supplied author is
    unverified, and its rule is that anonymous beats misattributed. A fabricated subject would be worse
    than none — a later bus gate would authorize it.

    ``uuids`` name the output's table and namespace in its ``rask_lance`` facet (ADR 0173 §10): a rename keeps a table's
    uuid and changes its name, so the feed keys the pass on the table it reclaimed, not on whatever holds the name later.
    """
    facets: dict[str, Any] = {LANCE_FACET: lance_facet(_PRODUCER, operation=EXPIRE_VERSIONS)}
    if author:
        facets[AUTHOR_FACET] = author_facet(_PRODUCER, author)
    return {
        "eventType": "COMPLETE",
        "eventTime": event_time,
        "producer": _PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA_URL,
        "run": {"runId": run_id, "facets": facets},
        # Per-table job identity (like the catalog write emitter) — else every dataset's expiry lumps into one job
        # whose output set spans the whole lakehouse.
        "job": {"namespace": job_namespace, "name": f"{EXPIRE_VERSIONS}.{table_id}"},
        "inputs": [],
        "outputs": [with_dataset_uuids({"namespace": namespace, "name": table_id}, uuids, producer=_PRODUCER)],
    }


@runtime_checkable
class MaintenanceEmitter(Protocol):
    """Emits expiry maintenance events (success + failure) to the lineage service (best-effort)."""

    async def emit_maintenance(self, *, table_id: str, namespace: str, uuids: DatasetUuids = UNRESOLVED) -> None: ...

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str, uuids: DatasetUuids = UNRESOLVED) -> None: ...


class NoopEmitter:
    """The emitter used when lineage emission is disabled (or unwired) — does nothing."""

    async def emit_maintenance(self, *, table_id: str, namespace: str, uuids: DatasetUuids = UNRESOLVED) -> None:
        return None

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str, uuids: DatasetUuids = UNRESOLVED) -> None:
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
        storage_options: Callable[[], StorageOptions] | None = None,
        author: str = "",
        signing: SigningKeyHolder[SigningKey] | None = None,
    ) -> None:
        if signing is not None and signing.identity != author:
            raise ValueError(
                f"this service signs as {signing.identity!r} but stamps {author!r} as its author: a verifier refuses a signer that is not its event's author"
            )
        self._client = client
        #: This service's OWN signing key ([[LH-064]]). None is a service with no store to sign from, which emits
        #: unsigned as it always did. A holder without a key emits nothing at all, never an unsigned event.
        self._signing = signing
        #: The service's OWN identity, stamped on every event it emits — the same string it presents at
        #: the catalog's service door, so the two stores agree about who did the work.
        self._author = author
        self._pubsub = pubsub
        self._topic = topic
        self._job_namespace = job_namespace
        self._timeout_seconds = timeout_seconds
        self._outbox_uri = outbox_uri
        #: Read at every stage, never held: the key pair in it rotates while the service runs ([[XC-082]]).
        self._storage_options = storage_options

    async def emit_maintenance(self, *, table_id: str, namespace: str, uuids: DatasetUuids = UNRESOLVED) -> None:
        # uuid4 ON PURPOSE: each tick that reclaims versions is a distinct successful run (§4 decided —
        # do NOT make COMPLETE deterministic; only the FAIL path below needs the flood guard).
        event = build_maintenance_event(
            table_id=table_id,
            namespace=namespace,
            job_namespace=self._job_namespace,
            run_id=str(uuid.uuid4()),
            event_time=datetime.now(UTC).isoformat(),
            author=self._author,
            uuids=uuids,
        )
        await self._publish(event, table_id)

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str, uuids: DatasetUuids = UNRESOLVED) -> None:
        # DETERMINISTIC run id — the flood guard: the cron re-sweeps every ~2 min, so a persistently
        # failing dataset would otherwise mint a fresh run, and a fresh feed row, per tick. One id per
        # dataset → the feed's key for a terminal state (`<run_id>@FAIL`) is the same on every tick, and its
        # insert-only merge lands the first FAIL row and nothing after. Accepted consequence (§4): across
        # DISTINCT failure episodes the feed keeps the FIRST FAIL row (its keep-first terminal contract)
        # until the feed's retention (`eventsRetentionDays`) prunes it; the next failure after that lands anew.
        event = build_maintenance_fail_event(
            table_id=table_id,
            namespace=namespace,
            job_namespace=self._job_namespace,
            run_id=run_id_for(f"{EXPIRE_VERSIONS}-fail-{table_id}"),
            event_time=datetime.now(UTC).isoformat(),
            error=error,
            author=self._author,
            uuids=uuids,
        )
        await self._publish(event, table_id)

    def _signed(self, event: dict[str, Any]) -> dict[str, Any]:
        """The event as it goes on the wire: signed as this service, or unchanged when it does not sign.

        ONE SIGNING POINT for the whole service, because every lineage event it emits passes here. Signing in the
        builders instead would need it in each of them and in every one added later, and a builder that forgot would
        emit something indistinguishable from a signed event until someone checked. The identity is the one this
        emitter stamps as `author`, so a verifier's signer-equals-author binding holds by construction.

        Raises:
            SigningKeyUnavailableError: this service signs and has no key.
            CanonError: the event has no canonical form.
        """
        if self._signing is None:
            return event
        return attach_signature(event, key=self._signing.key(), identity=self._signing.identity)

    async def _publish(self, event: dict[str, Any], table_id: str) -> None:
        try:
            # SIGNED INSIDE THE TRY: an emit is best-effort, so a signature that cannot be made is a withheld event,
            # logged, and never a failed sweep.
            event = self._signed(event)
            # STAGED, then published, then dropped on ack (#4) — the twin of the catalog's emit. A
            # sweep's lineage event describes a committed reclamation, so losing one leaves the feed
            # under-reporting work that really happened. Degrades to exactly the previous plain publish
            # when `outbox_uri` is unset, which is the default, and stays bounded either way so a hung
            # sidecar cannot stall the sweep.
            await outbox.publish_lineage_with_outbox(
                self._client,
                outbox_uri=self._outbox_uri,
                storage_options=self._storage_options() if self._storage_options is not None else {},
                run_id=str((event.get("run") or {}).get("runId") or ""),
                event_json=json.dumps(event),
                pubsub_name=self._pubsub,
                topic_name=self._topic,
                timeout_seconds=self._timeout_seconds,
            )
        except (SigningKeyUnavailableError, CanonError) as exc:
            log.warning("maintenance_event_withheld_unsigned", extra={"table": table_id, "error": str(exc)})
        except Exception as exc:
            log.warning("maintenance_publish_failed", extra={"table": table_id, "error": str(exc)})


def make_emitter(
    *,
    enabled: bool,
    dapr: DaprClient | None,
    outbox_uri: str = "",
    storage_options: Callable[[], StorageOptions] | None = None,
    pubsub: str,
    topic: str,
    job_namespace: str,
    timeout_seconds: float = 5.0,
    author: str = "",
    signing: SigningKeyHolder[SigningKey] | None = None,
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
            author=author,
            signing=signing,
        )
    return NoopEmitter()
