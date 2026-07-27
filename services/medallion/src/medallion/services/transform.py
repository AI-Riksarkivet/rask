"""The mover's stage-transform business logic — one DAG edge, infra-free + testable.

:func:`handle_stage` is the heart of a medallion mover: given one upstream stage trigger it emits the
transform's OpenLineage event (``inputs=[from_dataset]`` -> ``outputs=[to_dataset]`` — the ``DERIVED_FROM``
edge) and publishes the next stage's trigger, so a single producer event cascades raw->bronze->silver->gold.

Idempotent + best-effort: with ``compute_enabled`` the transform does a REAL in-process Lance write (the
fake-Ray compute) and the emit carries the real version; off, it's a pure lineage emit (version 1). The
graph MERGEs on run_id, and a compute/publish outage returns ``RETRY`` so the Dapr sidecar redelivers.
When the FGA gate is on, the mover
CHECKS it is authorized to produce the target stage as its own service identity before emitting — an
unauthorized mover returns ``DROP`` (redelivery won't grant the role), so the cascade enforces the ReBAC.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from common import dapr_publish, fga, outbox
from common.warehouse_registry import (
    UnresolvableProjectError,
    is_safe_project,
    project_gold_root,
    project_root,
)
from dapr.aio.clients import DaprClient
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from opentelemetry import trace

from medallion.core.config import MedallionSettings, project_namespace
from medallion.core.metrics import record_denied, record_quality_blocked, record_transition
from medallion.schemas.events import build_run_event
from medallion.services.compute import measure_stage, transform_stage
from medallion.services.derivers import UnderivableMediaError
from medallion.services.quality import Assertion, assert_quality, passed
from medallion.services.ray_submit import submit_stage_job


log = logging.getLogger(__name__)
# The Lance/S3 write runs in a threadpool and is invisible to every auto-instrumentor — a manual INTERNAL
# span makes the step that dominates wall-clock time visible inside the cascade's distributed trace.
tracer = trace.get_tracer(__name__)

# Single-flight guard for the stage WRITE. Each mover process moves exactly ONE target dataset, so a
# process-wide lock serializes concurrent handler invocations for that target — a redelivered trigger racing
# the original, or two overlapping ticks — preventing two `write_dataset(mode="overwrite")` (or two Ray jobs
# writing the same to_uri) from committing concurrently. With moverReplicas=1 (the default) this is
# maxConcurrency=1 for the stage cluster-wide; the write stays overwrite-idempotent so scaling replicas is
# still safe (last-writer-wins on identical deterministic content), the lock just removes the concurrent
# commit contention. Module-level: one lock per mover process, created without binding a loop (py3.10+).
_write_lock = asyncio.Lock()

_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}
_DROP = {"status": "DROP"}
# A quality-blocked run was handled (its failed assertions are recorded in lineage), it just must not
# promote — DROP so Dapr doesn't redeliver (the data is deterministically bad; no DLQ is configured,
# so the drop is final — the failed run in the lineage graph is the audit trail).
_QUALITY_BLOCKED = {"status": "DROP"}


async def handle_stage(dapr: DaprClient, settings: MedallionSettings, event: Any, *, fga_client: OpenFgaClient | None = None) -> dict[str, str]:
    """Handle one upstream stage trigger: emit the transform's lineage, then trigger the next stage.
    ``event`` is the untrusted Dapr CloudEvent envelope (hence ``Any`` + the ``isinstance`` guard).

    When ``fga_client`` is set (MEDALLION_FGA_ENABLED), the mover first CHECKS it is authorized to produce
    the target stage — ``can_promote`` for the silver->gold mover, ``can_create_table`` for the others — as
    its own service identity. Unauthorized -> ``DROP`` (redelivery won't grant the role): the cascade
    enforces the ReBAC, so a mover lacking the validator role genuinely cannot promote to gold.

    ``project`` on the trigger (#84 per-tenant routing, opt-in) resolves this stage's from/to URIs off
    the warehouse registry (``<project-root>/medallion/<namespace>``) and project-qualifies every lineage
    identity + the FGA object. FAIL CLOSED: an unsafe project or one arriving with resolution disabled
    (no ``MEDALLION_CONTROL_ROOT``) is DROPped; a project with no active warehouse records a FAIL run and
    DROPs — never a fallback to the default roots, which would transform the WRONG tenant's data while
    emitting real-looking lineage for it. No ``project`` → today's behavior, byte-identical."""
    data = event.get("data") if isinstance(event, dict) else None
    token = data.get("token") if isinstance(data, dict) else None
    transition = f"{settings.from_namespace}->{settings.to_namespace}"

    raw_project = data.get("project") if isinstance(data, dict) else None
    project = ""
    if raw_project is not None:
        if not is_safe_project(raw_project):
            # Deterministic garbage (would become an S3 prefix / lineage name) — DROP, never repair.
            log.warning("medallion_stage_bad_project", extra={"transition": transition, "token": token})
            return _DROP
        project = raw_project
    if project and not settings.control_root:
        # Fail closed (#84): with resolution disabled the default roots MUST NOT serve a tenant trigger.
        # Deterministic (redelivery won't configure the registry) → DROP, not RETRY.
        log.warning(
            "medallion_stage_project_routing_disabled",
            extra={"transition": transition, "token": token, "project": project},
        )
        return _DROP
    # Lineage + FGA identities — project-qualified when a tenant trigger, exactly the env values when not.
    from_namespace = project_namespace(project, settings.from_namespace)
    from_dataset = project_namespace(project, settings.from_dataset)
    to_namespace = project_namespace(project, settings.to_namespace)
    to_dataset = project_namespace(project, settings.to_dataset)

    if fga_client is not None:
        try:
            allowed = await fga.check(
                fga_client,
                user=settings.fga_service_identity,
                relation=settings.fga_required_action,
                obj=settings.fga_object(to_namespace),
            )
        except ServiceUnavailableError as exc:
            # An FGA OUTAGE is transient (unlike a denial): return the explicit RETRY contract so the
            # sidecar redelivers, instead of leaking a 500 that is only incidentally retriable.
            log.warning(
                "medallion_stage_fga_unavailable",
                extra={"transition": transition, "token": token, "error": str(exc)},
            )
            return _RETRY
        if not allowed:
            record_denied(transition)
            log.warning(
                "medallion_stage_denied",
                extra={
                    "transition": transition,
                    "identity": settings.fga_service_identity,
                    "action": settings.fga_required_action,
                    "object": settings.fga_object(to_namespace),
                },
            )
            return _DROP

    quality_blocked = False
    completed = False  # set once the COMPLETE lineage emit lands — gates the FAIL-on-failure below
    try:
        # #84: resolve THIS stage's roots for a tenant trigger — the registry read is blocking IO
        # (threadpool). No active warehouse is deterministic → the dedicated except below records the
        # FAIL run and DROPs; a transient registry outage raises IO errors into the generic RETRY path.
        from_uri, to_uri = settings.from_uri, settings.to_uri
        if project:
            root = await run_in_threadpool(project_root, settings.control_root, settings.storage_options(), project)
            if root is None:
                raise UnresolvableProjectError(f"project {project!r} has no active warehouse")
            from_uri = f"{root}/medallion/{settings.from_namespace}"
            to_uri = f"{root}/medallion/{settings.to_namespace}"
            if settings.gold_warehouse_enabled:
                # Gold tier (DECISIONS "Medallion tiers"): the chart sets this env ONLY on the terminal
                # silver→gold mover, whose tenant TARGET root becomes the project's gold SERVING
                # warehouse (the serving=="gold" registry record) when one exists. The upstream READ
                # stays in the work warehouse; no gold warehouse → fall through to the work root above,
                # byte-identically. Lineage/FGA identities are untouched — only the physical root moves.
                gold_root = await run_in_threadpool(project_gold_root, settings.control_root, settings.storage_options(), project)
                if gold_root is not None:
                    to_uri = f"{gold_root}/medallion/{settings.to_namespace}"
        # 0. Fake-Ray compute (opt-in): a REAL in-process Lance write of the downstream dataset, so the
        # emitted lineage carries the actual version + measured output statistics (rows + on-disk bytes),
        # and the cascade produces data, not just provenance. Blocking Lance/S3 IO → threadpool. Off →
        # version 1, no stats (dummy emit). A compute failure → RETRY below. With the quality gate on, the
        # mover then ASSERTS quality on the dataset it just wrote (the produced data is what's validated).
        result = None
        assertions: list[Assertion] = []
        if settings.compute_enabled and from_uri and to_uri:
            # Serialize the write (+ the quality read of what it just wrote) against a concurrent redelivery
            # of the same stage — single-flight so two overwrites can't race on the same target dataset.
            async with _write_lock:
                with tracer.start_as_current_span("medallion.transform") as span:
                    span.set_attribute("lance.medallion.transition", transition)
                    use_ray = settings.ray_enabled
                    if use_ray:
                        # EVENT-DRIVEN real-Ray: submit the stage transform to the Ray cluster IN RESPONSE
                        # TO this trigger (`ray job submit` via the Ray Jobs REST API), then measure the
                        # written dataset so the lineage emit matches the in-process path. A job
                        # failure/timeout raises → the except below RETRYs and the sidecar redelivers.
                        span.set_attribute("lance.medallion.compute", "ray")
                        await submit_stage_job(
                            settings,
                            from_uri=from_uri,
                            to_uri=to_uri,
                            stage=settings.to_namespace,
                            token=token,  # deterministic submission id → redelivery re-attaches (idempotent)
                        )
                        # measure_stage, not a bare measure: the Ray job transformed out-of-process, so the
                        # column edges are RECONSTRUCTED from the upstream + written schemas — otherwise the
                        # columnLineage facet would be empty on exactly the path production runs.
                        result = await run_in_threadpool(
                            measure_stage,
                            from_uri,
                            to_uri,
                            settings.storage_options(),
                        )
                    else:
                        if not settings.ray_enabled:  # the blob fallback above already named the path
                            span.set_attribute("lance.medallion.compute", "in_process")
                        result = await run_in_threadpool(
                            transform_stage,
                            from_uri,
                            to_uri,
                            settings.storage_options(),
                            stage=settings.to_namespace,
                        )
                    span.set_attribute("lance.version", result.version)
                    span.set_attribute("lance.row_count", result.row_count)
                    span.set_attribute("lance.size_bytes", result.size_bytes)
                if settings.quality_enabled:
                    assertions = await run_in_threadpool(
                        assert_quality,
                        to_uri,
                        settings.storage_options(),
                        key_column=settings.quality_key_column,
                        required_columns=settings.required_column_list,
                    )
        run_event = build_run_event(
            operation=settings.operation,
            author=settings.author,
            job_namespace=settings.job_namespace,
            inputs=[(from_namespace, from_dataset)],
            output_namespace=to_namespace,
            output_name=to_dataset,
            version=result.version if result else 1,
            row_count=result.row_count if result else None,
            size_bytes=result.size_bytes if result else None,
            source_uri=to_uri if result else None,
            schema_fields=result.fields if result else None,
            # Field-to-field column lineage (#1): the compute declares which upstream column each output
            # column came from — declared by the in-process transform, reconstructed from the on-disk schemas
            # on the Ray path — so the LIVE cascade populates the columnLineage graph (not just seed).
            column_map=result.column_map if result else None,
            # exclude_none: an assertion with no column omits the key entirely — a serialized
            # ``"column": null`` fails strict DataQualityAssertionsDatasetFacet validation (column: string).
            assertions=[a.model_dump(exclude_none=True) for a in assertions] or None,
            token=token,
            project=project or None,
        )
        # 1. Emit the transform's lineage DURABLY (#4): stage the full event in the object-store outbox,
        # publish, drop on ack — so a crash between the Lance commit above and this publish can't lose it
        # (the lineage relay re-ingests any staged survivor, idempotent on run_id). Degrades to a plain
        # publish when no outbox_uri is set. Runs even on a quality failure, so the failed assertions are
        # recorded and the bad batch stays auditable.
        await outbox.publish_lineage_with_outbox(
            dapr,
            outbox_uri=settings.lineage_outbox_uri,
            storage_options=settings.storage_options(),
            run_id=run_event["run"]["runId"],
            event_json=json.dumps(run_event),
            pubsub_name=settings.pubsub,
            topic_name=settings.lineage_topic,
            timeout_seconds=settings.publish_timeout_seconds,
        )
        completed = True  # the COMPLETE is recorded — a later trigger-publish failure is NOT a run failure
        # 2. Quality gate: a failed assertion BLOCKS promotion — record it, but do NOT trigger the next
        # stage, so a bad batch can't cascade. Composes with the FGA gate above (authz AND data-quality).
        if assertions and not passed(assertions):
            quality_blocked = True
        # 3. Trigger the next stage (unless terminal — gold has no pub_topic — or blocked by the gate).
        elif settings.pub_topic:
            next_trigger = {
                "token": token,
                "dataset": settings.to_dataset,
                "namespace": settings.to_namespace,
            }
            if project:  # #84: PROPAGATE the tenant down the cascade; omitted (byte-identical) when unset
                next_trigger["project"] = project
            await dapr_publish.publish_event(
                dapr,
                timeout_seconds=settings.publish_timeout_seconds,
                pubsub_name=settings.pubsub,
                topic_name=settings.pub_topic,
                data=json.dumps(next_trigger),
                data_content_type="application/json",
            )
    except UnresolvableProjectError as exc:
        # Deterministic (#84): redelivery cannot conjure an active warehouse for the project, so mirror
        # the quality-gate contract — record the FAIL run (the audit trail, idempotent on the
        # token-derived run_id) and DROP. NEVER fall back to the shared default roots.
        log.warning(
            "medallion_stage_project_unresolvable",
            extra={"transition": transition, "token": token, "project": project, "error": str(exc)},
        )
        with suppress(Exception):
            fail_event = build_run_event(
                operation=settings.operation,
                author=settings.author,
                job_namespace=settings.job_namespace,
                inputs=[(from_namespace, from_dataset)],
                output_namespace=to_namespace,
                output_name=to_dataset,
                token=token,
                project=project or None,
                event_type="FAIL",
                error_message=str(exc),
            )
            await outbox.publish_lineage_with_outbox(
                dapr,
                outbox_uri=settings.lineage_outbox_uri,
                storage_options=settings.storage_options(),
                run_id=fail_event["run"]["runId"],
                event_json=json.dumps(fail_event),
                pubsub_name=settings.pubsub,
                topic_name=settings.lineage_topic,
                timeout_seconds=settings.publish_timeout_seconds,
            )
        return _DROP
    except UnderivableMediaError as exc:
        # DETERMINISTIC bad media (a payload matched the content probe but cannot decode): redelivery
        # cannot fix bytes, so mirror the quality-gate contract — record the FAIL run (the audit trail,
        # idempotent on the token-derived run_id) and DROP instead of a pointless RETRY storm that would
        # re-read every blob from S3 up to maxDeliver times.
        record_quality_blocked(transition)
        log.warning(
            "medallion_media_underivable",
            extra={"transition": transition, "token": token, "error": str(exc)},
        )
        with suppress(Exception):
            fail_event = build_run_event(
                operation=settings.operation,
                author=settings.author,
                job_namespace=settings.job_namespace,
                inputs=[(from_namespace, from_dataset)],
                output_namespace=to_namespace,
                output_name=to_dataset,
                token=token,
                project=project or None,
                event_type="FAIL",
                error_message=str(exc),
            )
            # Through the OUTBOX (#4), like every other lineage emit. This path returns _DROP — Dapr will NOT
            # redeliver — so a lost FAIL publish means the failed run is NEVER recorded and NEVER retried:
            # the graph silently forgets it. Staging makes the failure durable. A staged FAIL is not a
            # phantom: the relay re-ingests a truthful "this run failed" record; it implies no committed data.
            await outbox.publish_lineage_with_outbox(
                dapr,
                outbox_uri=settings.lineage_outbox_uri,
                storage_options=settings.storage_options(),
                run_id=fail_event["run"]["runId"],
                event_json=json.dumps(fail_event),
                pubsub_name=settings.pubsub,
                topic_name=settings.lineage_topic,
                timeout_seconds=settings.publish_timeout_seconds,
            )
        return _DROP
    except Exception as exc:
        log.warning("medallion_stage_failed", extra={"transition": transition, "token": token, "error": str(exc)})
        # Record the failed run ONLY if the transform itself failed — i.e. the COMPLETE was never emitted.
        # A failure AFTER the COMPLETE (the downstream trigger publish) is NOT a run failure: the run
        # succeeded and its COMPLETE is already recorded; emitting a FAIL then would flip that successful
        # run to FAIL (and leave a spurious FAIL feed row). Such a case just RETRIES — redelivery re-emits
        # the idempotent COMPLETE + re-publishes the trigger. The FAIL RunEvent keeps a bare output (WROTE
        # edge, no version) + the errorMessage facet; best-effort + suppressed so it can't mask the RETRY;
        # idempotent on the deterministic run_id.
        if not completed:
            with suppress(Exception):
                fail_event = build_run_event(
                    operation=settings.operation,
                    author=settings.author,
                    job_namespace=settings.job_namespace,
                    inputs=[(from_namespace, from_dataset)],
                    output_namespace=to_namespace,
                    output_name=to_dataset,
                    token=token,
                    project=project or None,
                    event_type="FAIL",
                    error_message=str(exc),
                )
                # Through the OUTBOX (#4) — see the _DROP path above. Dapr DOES redeliver here, so a lost FAIL
                # is eventually re-emitted; staging it anyway keeps the invariant UNIFORM ("every lineage
                # publish is staged") rather than a special case that the next audit has to re-derive.
                await outbox.publish_lineage_with_outbox(
                    dapr,
                    outbox_uri=settings.lineage_outbox_uri,
                    storage_options=settings.storage_options(),
                    run_id=fail_event["run"]["runId"],
                    event_json=json.dumps(fail_event),
                    pubsub_name=settings.pubsub,
                    topic_name=settings.lineage_topic,
                    timeout_seconds=settings.publish_timeout_seconds,
                )
        return _RETRY
    if quality_blocked:
        record_quality_blocked(transition)
        log.warning(
            "medallion_quality_blocked",
            extra={"transition": transition, "token": token, "to": settings.to_dataset},
        )
        return _QUALITY_BLOCKED
    record_transition(transition)
    log.info("medallion_stage_moved", extra={"transition": transition, "token": token, "to": settings.to_dataset})
    return _SUCCESS
