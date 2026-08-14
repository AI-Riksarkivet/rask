"""Submit a stage-transform Ray job to the ray-lance cluster via the Ray Jobs REST API.

The event-driven real-Ray path (``MEDALLION_RAY_ENABLED``): a mover submits ``scripts/ray_stage_job.py``
(baked into the ray-lance image) to the Ray cluster IN RESPONSE TO its Dapr cascade trigger, instead of the
in-process fake-Ray ``compute.transform_stage``. Uses only ``httpx`` against the Ray Jobs REST API — no
``ray`` package in the mover image.

Idempotent under at-least-once redelivery: the submission id is DETERMINISTIC per (stage, token), so a
redelivered trigger RE-ATTACHES to the same job rather than starting a second concurrent job that would
race the write. A submit failure raises so the mover returns RETRY and the sidecar redelivers.

EVERY path is submit-and-ack since A13 (2026-08-03). The stage path used to block until the
job finished, which made the ack contract a race — a job outliving the redelivery window exhausted it —
and, more to the point, asked a question the data already answers: a job's completion signal is its own
registered commit through the catalog, and the publication event off that commit wakes the next tier.
A job that dies commits nothing and rings nothing; the lineage reconciler catches it against storage
truth. What was previously described here as the TRAIN path's "async-completion redesign" is now simply
how all three work. See docs/RESILIENCE.md + docs/RAY-TRAIN.md.
"""

from __future__ import annotations

import logging
import os

import httpx

from medallion.core.config import MedallionSettings
from ray_kit import submit as rk


log = logging.getLogger(__name__)

# Still live after A13: the TRAIN path reads it to decide re-attach vs already_failed
# (:237). Its sibling _TERMINAL_OK and the poll-error tolerance went with the completion
# poll — nothing observes a job to SUCCEEDED any more, so only the FAILED/STOPPED test
# survives, and only at submit time.
_TERMINAL_BAD = frozenset({"FAILED", "STOPPED"})


def stage_submission_id(stage: str, token: str | None, from_uri: str, to_uri: str) -> str:
    """The stage job's deterministic id, derived in ONE place.

    Extracted because S1's workflow has to name the same job twice — once to submit it, once to poll
    it — and a second inline copy of this expression is how the poller ends up watching an id the
    submitter never used, reporting a healthy job as missing forever.
    """
    return rk.submission_id(stage, token, work=f"{from_uri}\x00{to_uri}")


async def submit_stage_job(
    settings: MedallionSettings,
    *,
    from_uri: str,
    to_uri: str,
    stage: str,
    token: str | None,
    lineage_json: str = "",
) -> None:
    """Submit (or re-attach to) the stage transform on the Ray cluster and RETURN — never block.

    Raises :class:`RayJobError` on a submit failure, which the caller maps to RETRY. Completion is the
    job's own registered commit, not something observed from here.

    ``lineage_json`` is this run's consume-layer provenance document (R26). It rides the runtime_env so
    the job writes the ``lineage`` JSONB column in the SAME commit as the data — the distributed path
    must not produce a governed dataset the in-process path would have stamped. It is provenance, never
    a credential, so echoing it back through the jobs API (which mirrors runtime_env) is harmless.
    """
    # The work identity rides in the id: a token-less trigger used to collapse EVERY submission of a
    # stage onto `ray-<stage>-notoken`, and submit_or_reattach read the collision as success — the
    # second transform silently never ran. The same collapse hid WITH a token whenever one trigger
    # fans out to two tables of the same stage. from→to IS the transform's identity; a redelivered
    # trigger carries the same pair, so redelivery idempotency is unchanged.
    submission_id = stage_submission_id(stage, token, from_uri, to_uri)
    env_vars = {
        "FROM_URI": from_uri,
        "TO_URI": to_uri,
        "STAGE": stage,
        "LINEAGE_JSON": lineage_json,
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
        **rk.trace_env(),
    }
    body = {
        "entrypoint": settings.ray_entrypoint,
        "submission_id": submission_id,
        "runtime_env": {"env_vars": env_vars},
    }

    # SUBMIT-AND-ACK (A13, 2026-08-03) — the stage path no longer blocks on completion.
    #
    # It held the ack across the whole job runtime in a `while True: sleep()` completion poll
    # inside the HTTP request. Two things were wrong, and only the first is obvious. A job outliving
    # the redelivery window exhausted it, so the ack contract was a race the module docstring had to
    # describe rather than a property the code had. The second is why this is a DELETION rather than
    # a tuning exercise: nothing needs the poll. A job's completion signal is its own registered
    # commit through the catalog, and the publication event off that commit is what wakes the next
    # tier — polling was asking a question the data already answers.
    #
    # Holding an ack across a job's runtime is precisely what the ack contract forbids: ackWait
    # expires and the broker redelivers forever. A job that dies commits nothing and rings nothing;
    # the lineage reconciler catches it against storage truth, and the deterministic submission id
    # makes a redelivered trigger re-attach instead of starting a second job.
    async with httpx.AsyncClient(base_url=settings.ray_address, timeout=settings.ray_request_timeout_seconds) as client:
        await rk.submit_or_reattach(client, submission_id, body)
    log.info("ray_stage_job_submitted", extra={"submission_id": submission_id, "stage": stage})


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
    submission_id = rk.submission_id("train", token)
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
                **rk.trace_env(),
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
            raise rk.RayJobError(f"failed to submit ray train job {submission_id}: {exc}") from exc
    return "submitted"


#: Re-exported. The generic submitter moved to `ray_kit.submit` (R2) so `compute` — the execution
#: plane — can start jobs without importing the medallion. Callers keep this name.
RayJobError = rk.RayJobError
