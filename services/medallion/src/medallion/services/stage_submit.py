"""Submitting one stage of the cascade — WHAT to run, in no engine's vocabulary.

`docs/DECISIONS.md` "The compute plane is decoupled" ([[LH-159]]). The lakehouse must be driveable BY
Ray without depending ON it, so no module named for one engine sits on the cascade's submit path.

Nothing here names an engine except the dispatch's own answer. `transform.py` asks
`engine_choice.engine_for_async` which engine a stage runs on, and on the RAY branch `stage_plans` builds
the order here, plans it, and submits it here; `executor_for(RAY_ENGINE)` inside `submit_stage_order` is
therefore that branch's identity, not a hard-coded engine — the adapter is resolved BY NAME, because
naming `RayJobsApiExecutor` would swap one hard-coded engine for another and read like a fix.

`trace_env` and `otlp_env` live here rather than beside the Jobs API because neither is Ray's: one is
W3C context propagation and the other is this pod's OTLP configuration. They were parked in
engine-named modules, which is how a neutral helper comes to look like an engine's business.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from opentelemetry import propagate

from medallion.core.config import MedallionSettings
from medallion.services.engine_names import RAY_ENGINE
from medallion.services.engine_registry import executor_for
from medallion.services.planned_runs import outcome_url
from medallion.services.transform_spec import resolve_task_async, resolve_transform_async
from service_kit.lakehouse.executor import Executor, RunHandle
from service_kit.lakehouse.stage_stamp import ONE_TO_ONE
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.work_order import WorkDestination, WorkIdentity, WorkObservability, WorkOrder, WorkSource, WorkStamp, derive_idempotency_key


log = logging.getLogger(__name__)


def trace_env() -> dict[str, str]:
    """The current span's W3C trace context, in env-var shape for a job's ``runtime_env``.

    Trace continuity across the Ray boundary: the estate's distributed trace used to go dark at submit
    because no submission site propagated context, leaving every job-side span an orphan.
    ``propagate.inject`` writes nothing when no valid span is active, so this degrades to ``{}`` and the
    job runs untraced — the trace is only ever CONTINUED, never fabricated. Only the W3C keys are lifted;
    the global propagator also emits baggage, which has no reader on the job side.
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {k.upper(): v for k, v in carrier.items() if k in ("traceparent", "tracestate")}


#: The OTLP names this lane forwards from its own process into a job's `runtime_env`.
#: `TRACEPARENT`/`TRACESTATE` are deliberately absent: they are the active span's business and
#: `trace_env()` owns them.
_OTLP_NAMES: Final = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_HEADERS",
    # Spans ride GreptimeDB's trace pipeline — the chart sets a traces-specific header
    # (x-greptime-pipeline-name) the generic headers above do not carry.
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
)


def otlp_env() -> dict[str, str]:
    """This pod's OTLP configuration, with UNSET names omitted rather than blanked.

    AN EMPTY VALUE IS NOT AN ABSENCE HERE, and this file already records why for a different pair of
    keys: Ray merges `runtime_env` OVER the process env, so a key sent here BEATS the pod's. Forwarding
    `OTEL_EXPORTER_OTLP_ENDPOINT=""` therefore makes this submitter the OWNER of that key and disables
    tracing on a Ray cluster that has its own working configuration. Omitting it defers instead.

    The job's span belongs to the same logical service as the stage transform, which is why the service
    name is forwarded at all rather than left to the Ray pod.

    Read per call, never captured at import: a value snapshotted at module load is as old as the worker.
    """
    return {name: value for name in _OTLP_NAMES if (value := os.environ.get(name, ""))}


def build_stage_order_observability() -> WorkObservability:
    """This pod's trace context and OTLP config, as the ORDER carries them ([[LH-159]]).

    `WorkOrder.to_env` is documented as "The ONE serialization, so no adapter hand-rolls it", and the
    only reason this lane hand-rolled three spreads beside the order was that nothing ever populated
    `observability`. The values were always available; they were assembled in the wrong place.

    `otlp_env()` already OMITS a name this pod does not hold ([[XC-066]]) — which is what makes this a
    refactor rather than a behaviour change, since `to_env()` omits empty optionals too and the old
    block blanked them.
    """
    trace = trace_env()
    otlp = dict(otlp_env())
    return WorkObservability(
        traceparent=trace.get("TRACEPARENT", ""),
        tracestate=trace.get("TRACESTATE", ""),
        # POPPED, not copied: `to_env` emits the service name from its own field, and leaving it in the
        # map too would render one fact from two places — the divergence this whole change removes.
        service_name=otlp.pop("OTEL_SERVICE_NAME", ""),
        otlp=otlp,
    )


async def build_stage_order(
    settings: MedallionSettings,
    *,
    from_uri: str,
    to_uri: str,
    stage: str,
    token: str | None,
    lineage_json: str = "",
    originator: str = "",
    project: str = "",
    from_version: int | None = None,
    from_id: str = "",
    to_id: str = "",
    run_id: str = "",
) -> tuple[WorkOrder, TaskRegistration]:
    """The order one stage of the cascade submits, and the registration that says what running it means here.

    The order's ``idempotency_key`` is the run's one name (CP-029 clause a): the plan's action id, the engine's
    submission id and the outcome door's key. It is derived HERE and nowhere else, with ``code_version`` taken from
    the declaration when there is one and the chart otherwise, so a caller that cannot spell the key cannot spell
    it differently and a redelivery after a deploy names the new build's run.

    ``lineage_json`` is this run's consume-layer provenance document (R26). It rides the runtime_env so the job
    writes the ``lineage`` JSONB column in the SAME commit as the data. It is provenance, never a credential, so
    echoing it back through the jobs API (which mirrors runtime_env) is harmless.

    ``from_id``/``to_id``/``run_id`` are the run's PROVENANCE IDENTITY, and are not derivable from the two URIs
    beside them: a catalog identifier (``acme-silver$features``) is what the lineage graph and the FGA objects are
    keyed by, while a storage URI is a location. Empty is the UNWIRED case and omits the variable, leaving the
    runner's documented stem fallback in place.

    Raises:
        UndeclaredTransformError: the stage runner names a lane the catalog has no declaration for.
    """
    # A named-but-undeclared lane RAISES rather than falling back: a fallback would run the chart's
    # old program under the declaration's name. Unset lane keeps the chart settings.
    spec = await resolve_transform_async(settings, project=project)
    # THE DECLARATION NAMES A TASK; the REGISTRY says what running it means. Two reads rather than
    # one, and the split is the point: the record an operator writes carries no engine vocabulary, so
    # the same declaration is submittable by any plane that registered a task under that key. This
    # submitter runs Ray, so a task registered for anything else is refused here rather than handed
    # to the Jobs API as a command it cannot mean.
    entrypoint = (await resolve_task_async(settings, task=spec.task, engine=RAY_ENGINE)).command if spec else settings.ray_entrypoint
    job_params = spec.params if spec else settings.ray_job_params
    code_version = spec.code_version if spec else settings.ray_code_version
    # THE LANE'S ROW CARDINALITY. Chart lanes have never declared one and are 1:1, which is exactly
    # what the job's default enforces. A DECLARED lane can ask for a fan-out (one video into frames,
    # one recording into speaker turns), and this is the line that makes the declaration reach the job.
    cardinality = spec.cardinality if spec else ONE_TO_ONE
    key = derive_idempotency_key(stage=stage, token=token, from_uri=from_uri, to_uri=to_uri, code_version=code_version)
    # THE PLATFORM'S HALF OF THE CONTRACT, SERIALIZED ONCE. `WorkOrder.to_env()` is "the ONE serialization, so no
    # adapter hand-rolls it", pinned by `tests/unit/test_the_submitter_and_the_job_agree_on_the_wire.py`. The floor
    # is OMITTED when there is none rather than blanked, which `to_env` does for us.
    order = WorkOrder(
        task=spec.task if spec else stage,
        source=WorkSource(uri=from_uri, table_id=from_id, version_floor=from_version),
        destination=WorkDestination(uri=to_uri, table_id=to_id),
        # TOKEN AND TRANSFORM RIDE THE STAMP ([[LH-159]]): both are stamped as Ray job metadata and read back —
        # `rask.originator` recovers who a dead job was for, `rask.transform` names the declaration.
        stamp=WorkStamp(
            stage=stage,
            cardinality=cardinality,
            lineage_document=lineage_json,
            token=token or "",
            transform=spec.name if spec else "",
        ),
        identity=WorkIdentity(run_id=run_id, project=project, originator=originator, code_version=code_version),
        params=job_params,
        idempotency_key=key,
        outcome_url=outcome_url(settings, key),
        observability=build_stage_order_observability(),
    )
    # NO CREDENTIAL AND NO SHARED ENDPOINT RIDES THIS BODY, and it is the ORDER that guarantees it: `WorkOrder` carries
    # `extra="forbid"` with no field a credential could occupy. It is load-bearing because the Jobs API echoes
    # `runtime_env` on `GET /api/jobs/<id>` — an unauthenticated dashboard, proxied by compute at `/api/ray/*`.
    return order, TaskRegistration(task=order.task, engine=RAY_ENGINE, command=entrypoint, code_version=code_version)


async def submit_stage_order(order: WorkOrder, registration: TaskRegistration, *, executor: Executor | None = None) -> RunHandle:
    """Submit (or re-attach to) one planned stage and RETURN — never block on its completion.

    The job reports its own terminal state through the outcome door its order names, and the plan's sweep resolves
    one whose report never arrives, so nothing waits here (A13: holding an ack across a job's runtime is what the ack
    contract forbids). The engine posts the order's key as its submission id, so a redelivered order re-attaches to
    the run already doing the work.

    SUBMITTED THROUGH THE PORT, resolved BY NAME ([[LH-159]]) unless the caller already holds the engine (the sweep
    resubmits through the one it reads status from): naming `RayJobsApiExecutor` here would swap one hard-coded
    engine for another. `storage_options` is empty because this adapter needs none — the job resolves its own
    credentials from the pod.

    Raises:
        Exception: the engine could not be reached or refused the submission (the adapter's own error).
    """
    engine = executor if executor is not None else executor_for(RAY_ENGINE, storage_options={})
    handle, outcome = await engine.submit(order, registration)
    log.info(
        "ray_stage_job_submitted",
        extra={"submission_id": handle.handle, "stage": order.stamp.stage, "transform": order.stamp.transform, "outcome": outcome.value},
    )
    return handle
