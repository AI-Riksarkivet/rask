"""The medallion-producer producer's ingest business logic — the dummy BRONZE writer at the head of the pipeline.

:func:`produce` (with ``compute_enabled``) seeds a real ``bronze$events`` Lance dataset (R23: bronze is
the first governed tier — there is no raw dataset), then emits ONE OpenLineage event announcing that
write. It does NOT itself trigger the cascade — medallion-producer's own ``/bronze-arrival`` subscription
(:mod:`medallion.services.ingest_trigger`) reacts to that bronze-write event and publishes the
``medallion.bronze`` trigger, so the pipeline is driven by the bronze-arrival EVENT, not this call
(GOAL 4 B2). In production this is a real Ray Data job; here it is a dummy emitter, which is all the
event-driven demo needs.

Best-effort: a sidecar/broker outage logs + still returns (never 500s the producer) — the catalog contract.
"""

from __future__ import annotations

import json
import logging
import uuid
from functools import partial

from dapr.aio.clients import DaprClient
from fastapi.concurrency import run_in_threadpool
from opentelemetry import trace

from medallion.core.config import MedallionSettings, project_namespace
from medallion.schemas.events import build_run_event
from medallion.services.compute import seed_bronze
from service_kit.lakehouse import outbox
from service_kit.lakehouse.warehouse_registry import UnresolvableProjectError, project_root


log = logging.getLogger(__name__)
# Manual INTERNAL span over the threadpool Lance seed — auto-instrumentation can't see the compute step.
tracer = trace.get_tracer(__name__)


async def produce(
    dapr: DaprClient,
    settings: MedallionSettings,
    *,
    token: str | None = None,
    project: str = "",
    originator: str = "",
    rows: int | None = None,
) -> dict[str, str]:
    """Ingest the bronze dataset and emit its write event (the event-driven cascade head).

    With ``compute_enabled`` it FIRST seeds a real ``bronze$events`` Lance dataset (the fake medallion-producer
    ingest) so the emitted lineage carries the real version; off → a dummy emit (version 1). It then emits
    ONE OpenLineage event for ``bronze$events``. It does NOT publish ``medallion.bronze`` — medallion-producer's
    ``/bronze-arrival`` subscription reacts to this bronze-write event and fires the trigger, so the
    cascade is event-driven.
    ``rows`` (optional) sets the bronze volume this call writes. It exists because the review band
    compares a promotion's row count against its predecessor's, and a producer that always writes the
    same eight rows makes the delta permanently ZERO — no legal band can breach that, so the band was
    enabled, correct and unfalsifiable. It is also what §9.1's open question needs: the 0.25 default is
    ASSUMED, and measuring a real distribution takes promotions of different sizes. Absent → the
    seeder's own default, byte-identical to before.

    Best-effort: a sidecar/broker outage logs + still returns (the catalog-style contract).

    ``token`` is the caller's idempotency key (skill rule: an operation whose route invites retry must
    pair it with one): the route's 503 tells the caller to retry, but the publish timeout is ambiguous —
    the sidecar may have accepted the event before the timeout fired — so a retry that minted a FRESH
    token would double-fire the cascade head as two unrelated runs. A reused token converges instead:
    every downstream run_id derives from it, so the graph MERGEs the duplicate and the overwrite-writes
    land the same data. Absent (the common fire-and-forget case) → a fresh random token.

    ``project`` (#84 per-tenant routing, opt-in) routes the seed into that project's ACTIVE warehouse
    (``<root>/medallion/<bronze_namespace>``, resolved off the warehouse registry) and project-qualifies
    the emitted namespace/dataset, stamping ``project`` into the ``lance`` run facet so ``/bronze-arrival``
    can copy it onto the stage trigger. Fail closed: with routing disabled (no ``MEDALLION_CONTROL_ROOT``)
    or no active warehouse it raises :class:`UnresolvableProjectError` — never a fallback to the shared
    ``bronze_uri``. Empty (default) keeps today's behavior byte-identical."""
    token = token or uuid.uuid4().hex[:12]
    bronze_uri = settings.bronze_uri
    if project:
        if not settings.control_root:
            raise UnresolvableProjectError(f"project {project!r} produce refused: routing is disabled (MEDALLION_CONTROL_ROOT unset)")
        root = await run_in_threadpool(project_root, settings.control_root, settings.storage_options(), project)
        if root is None:
            raise UnresolvableProjectError(f"project {project!r} has no active warehouse")
        bronze_uri = f"{root}/medallion/{settings.bronze_namespace}"
    # The canonical name for the dataset this call writes — emitted as the event's `output_name` AND
    # stamped onto the dataset itself, from ONE expression so the two can never drift.
    bronze_dataset_id = project_namespace(project, settings.bronze_dataset)
    result = None
    if settings.compute_enabled and bronze_uri:
        # Fake-Ray ingest: a REAL Lance write of bronze$events (blocking IO → threadpool) → the real version
        # + the measured output statistics (rows + on-disk bytes) the emit records as outputStatistics.
        with tracer.start_as_current_span("medallion.produce") as span:
            # DECLARE the canonical name, exactly as every mover does (transform.py). `bronze_uri` is
            # composed from the NAMESPACE alone, so `medallion/bronze` is both `bronze$events` and
            # `bronze$pages` — the sweep cannot derive the id and, without this, emits no maintenance
            # provenance and no per-dataset FAIL event for the cascade HEAD.
            # `rows` is forwarded ONLY when given, so absent stays byte-identical to seed_bronze's own
            # default rather than restating it here — two copies of one number drift, and the drift is
            # invisible because both still "work".
            seed_kwargs: dict[str, object] = {"dataset_id": bronze_dataset_id}
            if rows is not None:
                seed_kwargs["rows"] = rows
            result = await run_in_threadpool(partial(seed_bronze, bronze_uri, settings.storage_options(), **seed_kwargs))
            span.set_attribute("lance.version", result.version)
            span.set_attribute("lance.row_count", result.row_count)
            span.set_attribute("lance.size_bytes", result.size_bytes)
    bronze_event = build_run_event(
        operation=settings.producer_operation,
        author=settings.producer_author,
        # The verified human from `authorize_produce`, carried so a failure LATER in the cascade can still
        # name them. `author` stays the producer role: it is the truthful answer to "what ran this".
        originator=originator or None,
        job_namespace=settings.job_namespace,
        inputs=[],  # the dummy seed has no external source; the ingest plane carries its own (R23)
        output_namespace=project_namespace(project, settings.bronze_namespace),
        output_name=bronze_dataset_id,
        version=result.version if result else 1,
        row_count=result.row_count if result else None,
        size_bytes=result.size_bytes if result else None,
        source_uri=bronze_uri if result else None,
        # The measured bronze$events schema (blob/vector-aware) so the cascade HEAD's WROTE edge records
        # real columns — seed_bronze already captured it in result.fields; measured but never emitted (#24).
        schema_fields=result.fields if result else None,
        token=token,
        project=project or None,
        # Same contract as every mover (see transform.py). Suppressing instead of marking is NOT an
        # option here: /bronze-arrival subscribes to this very event to publish medallion.bronze, so a
        # missing head event stops the cascade rather than merely thinning the graph.
        synthetic=result is None,
    )
    try:
        # The cascade HEAD is this bronze-write lineage event: medallion-producer's own /bronze-arrival subscription
        # reacts to it and publishes the medallion.bronze trigger, so the pipeline is driven by the arrival
        # EVENT, not by this call directly (event-driven head — the trigger publish moved to
        # ingest_trigger.py). Stage-then-publish-then-drop through the outbox (#4), same as every mover in
        # transform.py — so a crash between the bronze Lance commit and the publish ack leaves the cascade
        # HEAD's event staged for
        # the reconcile relay to recover (author + source_uri the version+schema back-fill can't reconstruct).
        # Degrades to a plain publish when lineage_outbox_uri is unset (the default). This makes the feature
        # uniform ("every lineage publish is staged") and the values.yaml claim literally true.
        await outbox.publish_lineage_with_outbox(
            dapr,
            outbox_uri=settings.lineage_outbox_uri,
            storage_options=settings.storage_options(),
            run_id=bronze_event["run"]["runId"],
            event_json=json.dumps(bronze_event),
            pubsub_name=settings.pubsub,
            topic_name=settings.lineage_topic,
            timeout_seconds=settings.publish_timeout_seconds,
        )
    except Exception as exc:
        log.warning("medallion_produce_failed", extra={"token": token, "error": str(exc)})
        return {"status": "publish_failed", "token": token}
    dataset = project_namespace(project, settings.bronze_dataset)
    log.info("medallion_produced", extra={"token": token, "dataset": dataset})
    return {"status": "produced", "token": token, "dataset": dataset}
