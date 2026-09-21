"""Submit a stage-transform Ray job to the ray-lance cluster via the Ray Jobs REST API.

The event-driven real-Ray path (``MEDALLION_RAY_ENABLED``): a stage runner submits ``scripts/ray_stage_job.py``
(baked into the ray-lance image) to the Ray cluster IN RESPONSE TO its Dapr cascade trigger, instead of the
in-process fake-Ray ``compute.transform_stage``. Uses only ``httpx`` against the Ray Jobs REST API — no
``ray`` package in the stage runner image.

Idempotent under at-least-once redelivery: the submission id is DETERMINISTIC per (stage, token), so a
redelivered trigger RE-ATTACHES to the same job rather than starting a second concurrent job that would
race the write. A submit failure raises so the stage runner returns RETRY and the sidecar redelivers.

EVERY path is submit-and-ack since A13 (2026-08-03). The stage path used to block until the
job finished, which made the ack contract a race — a job outliving the redelivery window exhausted it —
and, more to the point, asked a question the data already answers: a job's completion signal is its own
registered commit through the catalog, and the publication event off that commit wakes the next tier.
A job that dies commits nothing and rings nothing; the lineage reconciler catches it against storage
truth. What was previously described here as the TRAIN path's "async-completion redesign" is now simply
how all three work. See docs/RESILIENCE.md + docs/RAY-TRAIN.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

import httpx

from medallion.core.config import MedallionSettings, get_settings
from medallion.services import ray_jobs_api as rk
from medallion.services.stage_submit import trace_env


log = logging.getLogger(__name__)


#: The ONE Ray dashboard client for this worker process.
#:
#: Every submit used to open an `httpx.AsyncClient` in an `async with` and tear it down on the way out
#: — one TCP connect, one TLS handshake and one pool teardown per activity, on a durable workflow that
#: runs these repeatedly. `production-patterns.md`: "One engine, one HTTP client, per process."
#:
#: MODULE-LEVEL rather than lifespan-owned, and that is the honest shape rather than a shortcut: a
#: workflow ACTIVITY has no `Request` and no reachable `app.state`, so the reference's literal
#: prescription cannot apply. The client gets the WORKER's lifetime instead, and `close_ray_client()`
#: is called from the stage runner's shutdown — a module-level client nothing closes trades a per-call
#: teardown for a permanent leak plus an "Unclosed client session" on every stop.
#:
#: Guarded by a lock: two activities starting concurrently would otherwise both see `None` and build
#: two clients, one of which is then leaked with no reference to close it.
_client: httpx.AsyncClient | None = None
_client_address: str | None = None
_client_lock = asyncio.Lock()


async def ray_client() -> httpx.AsyncClient:
    """The pooled client, built on first use and rebuilt if the Ray address changes.

    KEYED ON THE ADDRESS, not merely cached. An `AsyncClient` binds `base_url` at construction, so a
    plain "build once" cache would keep answering with a client pointed at whatever address happened to
    be configured the FIRST time an activity ran — silently, and long after the setting changed. That is
    free in production, where the address is stable, and it is the difference between a cache and a
    stale global.
    """
    global _client, _client_address
    settings = get_settings()
    address = settings.ray_address
    if _client is not None and not getattr(_client, "is_closed", False) and _client_address == address:
        return _client
    async with _client_lock:
        if _client is None or getattr(_client, "is_closed", False) or _client_address != address:
            if _client is not None:
                with suppress(Exception):
                    await _client.aclose()
            _client = httpx.AsyncClient(base_url=address, timeout=settings.ray_request_timeout_seconds)
            _client_address = address
    return _client


async def close_ray_client() -> None:
    """Close the pooled client. Idempotent, so a double shutdown is not an error."""
    global _client, _client_address
    # TOLERANT ON PURPOSE, for the same reason the stage runner's teardown suppresses: a shutdown that raises
    # on an already-broken (or substituted) client must not stop the rest of the teardown. The refs are
    # dropped either way, so a failed close cannot leave a stale client answering later callers.
    if _client is not None:
        with suppress(Exception):
            await _client.aclose()
    _client = None
    _client_address = None


def train_submission_id(token: str) -> str:
    """The training job's deterministic id, derived in ONE place.

    Same reason as `stage_submission_id`: the SUBMITTER and the WATCHER must name the same job, and a
    second inline copy of this expression is exactly how a poller ends up watching an id nobody
    submitted — reporting a healthy training run as missing forever.
    """
    return rk.submission_id("train", token)


def stage_submission_id(stage: str, token: str | None, from_uri: str, to_uri: str, code: str = "") -> str:
    """The stage job's deterministic id, derived in ONE place.

    Extracted because S1's workflow has to name the same job twice — once to submit it, once to poll
    it — and a second inline copy of this expression is how the poller ends up watching an id the
    submitter never used, reporting a healthy job as missing forever. ``code`` (B3) must therefore
    reach BOTH calls or the poller watches an id the submitter never used — the exact defect the
    extraction exists to prevent, reintroduced through the new axis.
    """
    return rk.submission_id(stage, token, work=f"{from_uri}\x00{to_uri}", code=code)


async def submit_train_job(
    settings: MedallionSettings,
    *,
    model: str,
    features_json: str,
    config_json: str = "{}",
    token: str,
    registry_uri: str,
    artifact_base: str,
    originator: str = "",
    project: str = "",
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
    submission_id = train_submission_id(token)
    body = {
        "entrypoint": settings.train_entrypoint,
        "submission_id": submission_id,
        "runtime_env": {
            "env_vars": {
                "MODEL": model,
                "FEATURES": features_json,
                "CONFIG": config_json,
                # The job's OWN copy, because the job emits its own OpenLineage lifecycle (D2: no Dapr
                # sidecar on Ray pods) and is therefore the only writer that can stamp these onto the
                # events. `metadata` below serves the opposite need — reading the identity from
                # OUTSIDE, after a failure — and neither substitutes for the other.
                "ORIGINATOR": originator,
                "TRAIN_PROJECT": project,
                # NOT "TOKEN": lance's object-store env fallback reads a bare TOKEN as the AWS session
                # token, stamping x-amz-security-token on every S3 request → RustFS 500s (live 2026-07-13).
                "TRAIN_TOKEN": token,
                "MODELS_NAMESPACE": settings.models_namespace,
                # The D4 publish pointers (derived by the caller — layout convention lives in train.py)
                # + where the job posts its OWN OpenLineage lifecycle (D2: no Dapr sidecar on Ray pods).
                "REGISTRY_URI": registry_uri,
                "ARTIFACT_BASE": artifact_base,
                "LINEAGE_URL": settings.train_lineage_url,
                # The job authenticates to the lineage ingest as the SERVICE it already is (D5's
                # `service-trainer`) — but the shared app TOKEN no longer rides this dict. It did, and
                # the old NOTE here conceded the exposure out loud: the Jobs API echoes runtime_env
                # back. Fixed 2026-08-28 exactly as that note prescribed — a secret mounted on the Ray
                # pods (secretKeyRef; see chart/templates/rayservice.yaml) — so the job still reads
                # LINEAGE_SERVICE_TOKEN and S3_SECRET from `os.environ`, now sourced from the pod.
                # Empty/absent token (dev/auth-off) → header omitted → the ingest stays open.
                "LINEAGE_SERVICE_ID": settings.trainer_identity,
                # NO S3 NAME RIDES THIS BODY either — same rule and same owner as the stage lane
                # above, and `scripts/ray_train_job.py:20` documents the same required set. The pod
                # supplies all four.
                # Forward this pod's own OTLP config so the training job's metrics land in the same
                # GreptimeDB the services use (#18 experiment tracking → Perses). Empty (observability
                # off) → the job's emit_metrics is a no-op. The service name is the trainer's identity so
                # the metrics attribute to the trainer, not the submitting medallion-producer pod.
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
                **trace_env(),
            }
        },
        # Ray's own `metadata`, mirroring the stage path: this is what `GET /api/jobs/<id>` returns, so
        # it is the one place a failure investigated from outside the job can still recover WHO the run
        # was for — after the pod is gone, when the job emitted nothing because it died before its own
        # FAIL. Empty values are omitted; `""` must never be read back as an identity. Ray types
        # metadata as `Dict[str, str]`, which every value here already is.
        "metadata": {key: value for key, value in (("rask.originator", originator), ("rask.project", project), ("rask.token", token)) if value},
    }
    client = await ray_client()
    # THROUGH THE KERNEL, with D2 as the explicit policy: `on_terminal_failure="report"` is the one
    # deliberate divergence from the stage contract (the kernel's docstring names both). This
    # replaced an inline copy of the same POST-then-reattach dance — the "second implementation"
    # ray_jobs_api's header warns about, written before the kernel existed and never collapsed.
    outcome = await rk.submit_or_reattach(client, submission_id, body, on_terminal_failure="report")
    if outcome == "submitted":
        log.info("ray_train_job_submitted", extra={"submission_id": submission_id, "model": model})
    # The published contract keeps its historical strings: callers branch on "attached".
    return "attached" if outcome == "reattached" else outcome


#: Re-exported. The generic submitter lives in `ray_jobs_api` — this service's Ray ADAPTER — so `compute` — the execution
#: plane — can start jobs without importing the medallion. Callers keep this name.
RayJobError = rk.RayJobError
