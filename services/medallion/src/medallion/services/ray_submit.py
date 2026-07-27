"""Submit a stage-transform Ray job to the ray-lance cluster via the Ray Jobs REST API.

The event-driven real-Ray path (``MEDALLION_RAY_ENABLED``): a mover submits ``scripts/ray_stage_job.py``
(baked into the ray-lance image) to the Ray cluster IN RESPONSE TO its Dapr cascade trigger, instead of the
in-process fake-Ray ``compute.transform_stage``. Uses only ``httpx`` against the Ray Jobs REST API — no
``ray`` package in the mover image.

Idempotent under at-least-once redelivery: the submission id is DETERMINISTIC per (stage, token), so a
redelivered trigger (the handler blocks until the job finishes, which can exceed the 30s ack window)
RE-ATTACHES to the same job and polls it, rather than starting a second concurrent job that would race the
write. A failure (submit error, FAILED job, or timeout) raises so the mover returns RETRY and the sidecar
redelivers; on redelivery a terminally FAILED/STOPPED job with the same id is DELETED and resubmitted fresh
(so the retry runs on a healthy worker rather than re-observing the same failure), while a still-running job
is re-attached and polled. Production KubeRay handles in-job task retry/orchestration.

Known limitation (STAGE path only): ``submit_stage_job`` blocks until the job finishes, so a job that
outlives the redelivery window exhausts it — it suits bounded-duration stage transforms. The window depends
on the deploy: with the DEFAULT ``dapr.resiliency.enabled=true`` the sidecar owns retries and the broker
crash-recovery ackWait is 720s (ample vs the 180s job timeout); only the ``resiliency=false`` escape hatch
reverts to the broker-only ~2.5 min (30s ackWait × maxDeliver 5) this note previously described. The
TRAIN path (``submit_train_job``, #115a) is exactly the async-completion redesign this paragraph used to
call future work: submit-and-ack, the job emits its own lifecycle, and — unlike the stage
path — a terminally FAILED prior job is NEVER deleted-and-resubmitted. The two functions deliberately share
``_submission_id`` but keep separate submit protocols (accepted #115a deviation from "extract one core":
their re-attach semantics differ at the terminal-failure branch; if you fix the shared POST/GET protocol in
one, mirror it in the other). See docs/RESILIENCE.md + docs/RAY-TRAIN.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping

import httpx
from opentelemetry import propagate

from medallion.core.config import MedallionSettings


log = logging.getLogger(__name__)

_TERMINAL_OK = "SUCCEEDED"
_TERMINAL_BAD = frozenset({"FAILED", "STOPPED"})
# Tolerate a few transient poll blips (a 5xx / connect timeout) before giving up, so one bad GET doesn't
# abandon an in-flight job and trigger a redelivery that re-attaches anyway — bounded by the job timeout.
_MAX_POLL_ERRORS = 3


class RayJobError(RuntimeError):
    """A submitted Ray stage job failed, was stopped, or did not finish within the timeout."""


def _submission_id(stage: str, token: str | None) -> str:
    """A deterministic id per (stage, token) so redelivery re-attaches to the same job (idempotency)."""
    raw = f"ray-{stage}-{token or 'notoken'}"
    return re.sub(r"[^A-Za-z0-9_-]", "-", raw)[:200]


def _trace_env() -> dict[str, str]:
    """The current span's W3C trace context, lifted to env-var shape for the job's ``runtime_env``.

    Trace continuity across the Ray boundary (prod-readiness P3): the estate's distributed trace went
    dark at ``ray job submit`` because neither submission site propagated context — the job-side spans
    were orphans. ``propagate.inject`` writes nothing when no valid span is active (tracing off, unit
    tier), so this degrades to an empty dict and the job runs untraced — the trace is only ever
    CONTINUED, never fabricated. Only the W3C keys are lifted (the global propagator also emits
    baggage, which has no reader on the job side)."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {key.upper(): value for key, value in carrier.items() if key in ("traceparent", "tracestate")}


async def submit_stage_job(settings: MedallionSettings, *, from_uri: str, to_uri: str, stage: str, token: str | None) -> None:
    """Submit (or re-attach to) the stage transform on the Ray cluster and block until it succeeds.

    Raises :class:`RayJobError` on a submit failure, a FAILED/STOPPED job, or a timeout — the caller maps
    that to RETRY. On success the downstream Lance dataset exists at ``to_uri`` and the caller measures it.
    """
    submission_id = _submission_id(stage, token)
    env_vars = {
        "FROM_URI": from_uri,
        "TO_URI": to_uri,
        "STAGE": stage,
        "S3_ENDPOINT": settings.s3_endpoint,
        "S3_KEY": settings.s3_access_key_id,
        "S3_SECRET": settings.s3_secret_access_key.get_secret_value(),
        "S3_REGION": settings.s3_region,
        # Forward this pod's OTLP config (the train path below already does) so the job can export the
        # span it parents on the handed-over trace context. The service name is the mover's own — the
        # job executes that mover's stage transform, so its span belongs to the same logical service.
        # Empty endpoint (observability off) → the job runs untraced.
        "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        "OTEL_EXPORTER_OTLP_PROTOCOL": os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", ""),
        "OTEL_EXPORTER_OTLP_HEADERS": os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""),
        # Spans ride GreptimeDB's trace pipeline — the chart sets a traces-specific header
        # (x-greptime-pipeline-name) the generic headers above don't carry.
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS", ""),
        "OTEL_SERVICE_NAME": os.environ.get("OTEL_SERVICE_NAME", ""),
        "OTEL_RESOURCE_ATTRIBUTES": os.environ.get("OTEL_RESOURCE_ATTRIBUTES", ""),
        # Trace continuity (prod-readiness P3): the mover's active span rides the runtime_env as
        # TRACEPARENT, and the job starts its root span as a child of it — the cascade's distributed
        # trace no longer goes dark at `ray job submit`. Empty when no span is active.
        **_trace_env(),
    }
    body = {
        "entrypoint": settings.ray_entrypoint,
        "submission_id": submission_id,
        "runtime_env": {"env_vars": env_vars},
    }

    async with httpx.AsyncClient(base_url=settings.ray_address, timeout=settings.ray_request_timeout_seconds) as client:
        await _submit_or_reattach(client, submission_id, body)
        log.info("ray_stage_job_submitted", extra={"submission_id": submission_id, "stage": stage})
        try:
            async with asyncio.timeout(settings.ray_job_timeout_seconds):
                await _await_success(client, submission_id, settings.ray_poll_interval_seconds)
        except TimeoutError as exc:
            raise RayJobError(f"ray stage job {submission_id} did not finish within {settings.ray_job_timeout_seconds}s") from exc
    log.info("ray_stage_job_succeeded", extra={"submission_id": submission_id, "stage": stage})


async def _submit_or_reattach(client: httpx.AsyncClient, submission_id: str, body: Mapping[str, object]) -> None:
    """POST /api/jobs/. A 4xx when the id already exists (idempotent redelivery) re-attaches to that job —
    UNLESS that prior job terminally FAILED/STOPPED, in which case it is deleted and resubmitted fresh so the
    redelivery actually retries the transform on a healthy worker (instead of re-observing the same failure
    every redelivery until maxDeliver silently drops the trigger — the deterministic-id poison)."""
    try:
        response = await client.post("/api/jobs/", json=dict(body))
        if response.status_code < 400:
            return
        # Already-submitted (redelivery): inspect the existing job to decide re-attach vs fresh retry.
        existing = await client.get(f"/api/jobs/{submission_id}")
        if existing.status_code == 200:
            status = existing.json().get("status")
            if status in _TERMINAL_BAD:
                # DELETE is only valid on a terminal job (FAILED/STOPPED are), then re-POST the same id.
                await client.delete(f"/api/jobs/{submission_id}")
                fresh = await client.post("/api/jobs/", json=dict(body))
                fresh.raise_for_status()
                log.info("ray_stage_job_resubmitted_after_failure", extra={"submission_id": submission_id})
                return
            log.info("ray_stage_job_reattach", extra={"submission_id": submission_id})
            return
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RayJobError(f"failed to submit ray stage job {submission_id}: {exc}") from exc


async def _await_success(client: httpx.AsyncClient, submission_id: str, poll_interval: float) -> None:
    """Poll GET /api/jobs/{id} until SUCCEEDED; raise on FAILED/STOPPED. Bounded by the caller's timeout."""
    poll_errors = 0
    while True:
        await asyncio.sleep(poll_interval)
        try:
            response = await client.get(f"/api/jobs/{submission_id}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            poll_errors += 1
            if poll_errors > _MAX_POLL_ERRORS:
                raise RayJobError(f"failed to poll ray stage job {submission_id}: {exc}") from exc
            continue
        poll_errors = 0
        payload = response.json()
        status = payload.get("status")
        if status == _TERMINAL_OK:
            return
        if status in _TERMINAL_BAD:
            raise RayJobError(f"ray stage job {submission_id} {status}: {payload.get('message')}")


async def submit_train_job(
    settings: MedallionSettings,
    *,
    model: str,
    features_json: str,
    config_json: str = "{}",
    token: str,
    registry_uri: str,
    artifact_base: str,
) -> str:
    """SUBMIT-AND-ACK for a TRAINING job (docs/RAY-TRAIN.md D2) — never block on completion.

    Training is the "genuinely long job" the module docstring's limitation names, so this path inverts
    the stage contract: submit (or re-attach to) the job and RETURN — the JOB emits its own OpenLineage
    lifecycle; the caller acks the trigger immediately. Deterministic ``ray-train-<token>`` id = the
    redelivery idempotency key. Unlike the stage path, a terminally FAILED prior job is **NOT** deleted
    and resubmitted (D2: training compute is expensive; a failed run is terminal until a human POSTs
    /train with a fresh token) — it returns ``"already_failed"`` so the handler can DROP, attributably.
    Every HTTP call is bounded by ``ray_request_timeout_seconds``, keeping the handler inside the 30s
    Dapr ack window. Returns ``"submitted"`` | ``"attached"`` | ``"already_failed"``; raises
    :class:`RayJobError` on transport/submit errors (the handler maps that to RETRY).
    """
    submission_id = _submission_id("train", token)
    body = {
        "entrypoint": settings.train_entrypoint,
        "submission_id": submission_id,
        "runtime_env": {
            "env_vars": {
                "MODEL": model,
                "FEATURES": features_json,
                "CONFIG": config_json,
                # NOT "TOKEN": lance's object-store env fallback reads a bare TOKEN as the AWS session
                # token, stamping x-amz-security-token on every S3 request → RustFS 500s (live 2026-07-13).
                "TRAIN_TOKEN": token,
                "MODELS_NAMESPACE": settings.models_namespace,
                # The D4 publish pointers (derived by the caller — layout convention lives in train.py)
                # + where the job posts its OWN OpenLineage lifecycle (D2: no Dapr sidecar on Ray pods).
                "REGISTRY_URI": registry_uri,
                "ARTIFACT_BASE": artifact_base,
                "LINEAGE_URL": settings.train_lineage_url,
                # The job authenticates to the lineage ingest as the SERVICE it already is: the shared app
                # token + its bare FGA subject (D5's `service-trainer`). Without this every training
                # RunEvent 401'd under auth.enabled and ALL training provenance was silently lost (live
                # 2026-07-13). Empty app token (dev/auth-off) → header omitted → the ingest stays open.
                # NOTE the token rides in the Ray runtime_env, which the Ray Jobs API echoes back — the
                # SAME exposure the S3 credentials below already have. Tighten both together (a secret
                # mounted on the Ray pods) at the KubeRay merge; see docs/RAY-TRAIN.md D2.
                "LINEAGE_SERVICE_TOKEN": os.environ.get("APP_API_TOKEN", ""),
                "LINEAGE_SERVICE_ID": settings.trainer_identity,
                "S3_ENDPOINT": settings.s3_endpoint,
                "S3_KEY": settings.s3_access_key_id,
                "S3_SECRET": settings.s3_secret_access_key.get_secret_value(),
                "S3_REGION": settings.s3_region,
                # Forward this pod's own OTLP config so the training job's metrics land in the same
                # GreptimeDB the services use (#18 experiment tracking → Perses). Empty (observability
                # off) → the job's emit_metrics is a no-op. The service name is the trainer's identity so
                # the metrics attribute to the trainer, not the submitting lance-ray pod.
                "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
                "OTEL_EXPORTER_OTLP_PROTOCOL": os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", ""),
                "OTEL_EXPORTER_OTLP_HEADERS": os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""),
                # Spans ride GreptimeDB's trace pipeline — the chart sets a traces-specific header
                # (x-greptime-pipeline-name) the generic headers above don't carry.
                "OTEL_EXPORTER_OTLP_TRACES_HEADERS": os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS", ""),
                "OTEL_SERVICE_NAME": settings.trainer_identity,
                # Same resource attrs as the submitting pod (deployment env / namespace / version), so the
                # trainer's series carry the estate's standard resource dimensions, not a bare service name.
                "OTEL_RESOURCE_ATTRIBUTES": os.environ.get("OTEL_RESOURCE_ATTRIBUTES", ""),
                # Trace continuity (prod-readiness P3): the consumer's active span rides the runtime_env
                # as TRACEPARENT, and the job starts its root span as a child of it — the training run's
                # spans join the submitting trace instead of orphaning. Empty when no span is active.
                **_trace_env(),
            }
        },
    }
    async with httpx.AsyncClient(base_url=settings.ray_address, timeout=settings.ray_request_timeout_seconds) as client:
        try:
            response = await client.post("/api/jobs/", json=body)
            if response.status_code < 400:
                log.info("ray_train_job_submitted", extra={"submission_id": submission_id, "model": model})
                return "submitted"
            existing = await client.get(f"/api/jobs/{submission_id}")
            if existing.status_code == 200:
                if existing.json().get("status") in _TERMINAL_BAD:
                    log.warning("ray_train_job_previously_failed", extra={"submission_id": submission_id})
                    return "already_failed"
                log.info("ray_train_job_reattach", extra={"submission_id": submission_id})
                return "attached"
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RayJobError(f"failed to submit ray train job {submission_id}: {exc}") from exc
    return "submitted"
