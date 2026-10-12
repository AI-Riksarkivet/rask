"""Submitting one stage of the cascade — WHAT to run, in no engine's vocabulary.

`docs/adr/0052-the-compute-plane-is-decoupled-a-port-two-adapters-and-no.md` "The compute plane is decoupled" ([[LH-159]]). The lakehouse must be driveable BY
Ray without depending ON it, so no module named for one engine sits on the cascade's submit path.

Nothing here names an engine: `transform.py` asks `engine_choice.engine_for_async` which engine a stage runs on and
passes the answer in. On an out-of-process engine `stage_plans` builds the order here, plans it, and submits it
through the executor it resolved by name (CP-056).

`trace_env` lives here because W3C context propagation is no engine's.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import propagate

from medallion.core.config import MedallionSettings
from medallion.services.planned_runs import outcome_url
from medallion.services.transform_spec import resolve_task_async, resolve_transform_async
from service_kit.lakehouse.executor import Executor, RunHandle
from service_kit.lakehouse.stage_stamp import ONE_TO_ONE
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.transform_specs import TransformSpec
from service_kit.lakehouse.work_order import WorkDestination, WorkIdentity, WorkObservability, WorkOrder, WorkSource, WorkStamp, derive_idempotency_key


log = logging.getLogger(__name__)


def trace_env() -> dict[str, str]:
    """The current span's W3C trace context, in env-var shape for a job's ``runtime_env``.

    Trace continuity across the engine boundary: without it every job-side span is an orphan.
    ``propagate.inject`` writes nothing when no valid span is active, so this degrades to ``{}`` and the
    job runs untraced — the trace is only ever CONTINUED, never fabricated. Only the W3C keys are lifted;
    the global propagator also emits baggage, which has no reader on the job side.
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {k.upper(): v for k, v in carrier.items() if k in ("traceparent", "tracestate")}


def build_stage_order_observability() -> WorkObservability:
    """This pod's trace context and service name, as the ORDER carries them ([[LH-159]]).

    No OTLP endpoint or header rides the order: the pod that runs the job owns its exporter configuration
    (`WorkObservability`). The service name is read per call and OMITTED when this pod holds none ([[XC-066]]): an
    empty value in Ray's override layer would blank the job pod's own.
    """
    trace = trace_env()
    return WorkObservability(
        traceparent=trace.get("TRACEPARENT", ""),
        tracestate=trace.get("TRACESTATE", ""),
        service_name=os.environ.get("OTEL_SERVICE_NAME", ""),
    )


def build_work_order(
    settings: MedallionSettings,
    *,
    engine: str,
    spec: TransformSpec | None,
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
) -> WorkOrder:
    """The order one stage of the cascade runs under, in no engine's vocabulary: the ONE builder both lanes use.

    The in-process lane hands it to `InProcessExecutor`, an out-of-process lane plans and submits it, and each engine
    reads the same fields, so a trigger's delta boundary, cardinality and run key mean one thing on either. ``spec`` is
    the lane's resolved declaration, or ``None`` when this stage runner declares no transform and the chart's settings
    stand in for one: the task `MEDALLION_STAGE_TASK`, the params `MEDALLION_STAGE_PARAMS`, and the code version of
    ``engine`` (its image, or this lakehouse image for the in-process engine).

    The order's ``idempotency_key`` is the run's one name (CP-029 clause a): the plan's action id, the engine's
    submission id, the outcome door's key and the commit marker's action id. It is derived HERE and nowhere else,
    with ``code_version`` taken from the declaration when there is one and the chart otherwise, so a caller that
    cannot spell the key cannot spell it differently and a redelivery after a deploy names the new build's run.

    ``lineage_json`` is this run's consume-layer provenance document (R26), written as the ``lineage`` JSONB column
    in the same commit as the data. It is provenance, never a credential, so echoing it back through an engine's API
    is harmless.

    ``from_id``/``to_id``/``run_id`` are the run's PROVENANCE IDENTITY, and are not derivable from the two URIs
    beside them: a catalog identifier (``acme-silver$features``) is what the lineage graph and the FGA objects are
    keyed by, while a storage URI is a location. Empty is the UNWIRED case and omits the variable.

    ``from_version`` is the delta boundary. The order OMITS the floor when there is none rather than blanking it,
    which `to_env` does for us: a consumer reads a missing floor as "full scan".
    """
    code_version = spec.code_version if spec else settings.engine_code_version(engine)
    key = derive_idempotency_key(stage=stage, token=token, from_uri=from_uri, to_uri=to_uri, code_version=code_version)
    return WorkOrder(
        task=spec.task if spec else settings.stage_task,
        source=WorkSource(uri=from_uri, table_id=from_id, version_floor=from_version),
        destination=WorkDestination(uri=to_uri, table_id=to_id),
        # TOKEN AND TRANSFORM RIDE THE STAMP ([[LH-159]]): an engine stamps both as job metadata and reads them back —
        # `rask.originator` recovers who a dead job was for, `rask.transform` names the declaration.
        stamp=WorkStamp(
            stage=stage,
            # THE LANE'S ROW CARDINALITY. Chart lanes have never declared one and are 1:1, which is exactly what the
            # write's default enforces. A DECLARED lane can ask for a fan-out (one video into frames, one recording
            # into speaker turns), and this is the line that makes the declaration reach the engine.
            cardinality=spec.cardinality if spec else ONE_TO_ONE,
            lineage_document=lineage_json,
            token=token or "",
            transform=spec.name if spec else "",
        ),
        identity=WorkIdentity(run_id=run_id, project=project, originator=originator, code_version=code_version),
        params=spec.params if spec else settings.stage_params,
        idempotency_key=key,
        outcome_url=outcome_url(settings, key),
        observability=build_stage_order_observability(),
    )


async def build_stage_order(
    settings: MedallionSettings,
    *,
    engine: str,
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
    """The order an out-of-process lane plans for one stage, and the registration that says what running it means there.

    The order is :func:`build_work_order`'s; this adds the lane's declaration, resolved here, and the registration of
    its task for ``engine``. An undeclared lane's registration carries no command: the engine resolves the task to its
    own command, so the lakehouse holds no command line for it.

    Raises:
        UndeclaredTransformError: the stage runner names a lane the catalog has no declaration for.
        UnrunnableTaskError: the declared task is unregistered, or registered for another engine.
    """
    # A named-but-undeclared lane RAISES rather than falling back: a fallback would run the chart's
    # old program under the declaration's name. Unset lane keeps the chart settings.
    spec = await resolve_transform_async(settings, project=project)
    declared = await resolve_task_async(settings, task=spec.task, engine=engine) if spec else None
    order = build_work_order(
        settings,
        engine=engine,
        spec=spec,
        from_uri=from_uri,
        to_uri=to_uri,
        stage=stage,
        token=token,
        lineage_json=lineage_json,
        originator=originator,
        project=project,
        from_version=from_version,
        from_id=from_id,
        to_id=to_id,
        run_id=run_id,
    )
    # NO CREDENTIAL AND NO SHARED ENDPOINT RIDES THE ORDER: `WorkOrder` carries `extra="forbid"` with no field a
    # credential could occupy, and an engine may echo what it was sent (the Ray Jobs API echoes `runtime_env`).
    command = declared.command if declared is not None else ""
    return order, TaskRegistration(task=order.task, engine=engine, command=command, code_version=order.identity.code_version)


async def submit_stage_order(order: WorkOrder, registration: TaskRegistration, *, executor: Executor) -> RunHandle:
    """Submit (or re-attach to) one planned stage and RETURN — never block on its completion.

    The job reports its own terminal state through the outcome door its order names, and the plan's sweep resolves
    one whose report never arrives, so nothing waits here (A13: holding an ack across a job's runtime is what the ack
    contract forbids). The engine runs the order under its key, so a redelivered order re-attaches to the run
    already doing the work.

    Raises:
        Exception: the engine could not be reached or refused the submission (`EngineError` from the port).
    """
    handle, outcome = await executor.submit(order, registration)
    log.info(
        "stage_job_submitted",
        extra={
            "engine": handle.engine,
            "submission_id": handle.handle,
            "stage": order.stamp.stage,
            "transform": order.stamp.transform,
            "outcome": outcome.value,
        },
    )
    return handle
