"""Submitting one stage of the cascade — WHAT to run, in no engine's vocabulary.

`docs/DECISIONS.md` "The compute plane is decoupled" ([[LH-159]]). The lakehouse must be driveable BY
Ray without depending ON it, and the last thing standing in the way of that claim was an import: the
workflow reached its submitter through `ray_submit`, so a module named for one engine sat on the
cascade's only submit path.

Nothing here names an engine except the dispatch's own answer. `transform.py` asks
`engine_choice.engine_for_async` which engine a stage runs on and calls this module on the RAY branch;
`executor_for(RAY_ENGINE)` inside `submit_stage` is therefore that branch's identity, not a hard-coded
engine — the adapter is resolved BY NAME, because naming `RayJobsApiExecutor` would swap one
hard-coded engine for another and read like a fix.

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
from medallion.services.transform_spec import resolve_task_async, resolve_transform_async
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


async def submit_stage_job(
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
) -> str:
    """Submit (or re-attach to) one stage of the cascade and RETURN — never block.

    Returns THE HANDLE THE EXECUTOR SUBMITTED UNDER, and the caller must poll that value rather than
    deriving its own. The id is deterministic, so a second derivation reads as equivalent and is not:
    every axis has to be threaded to both sites, and when `code` (the build digest) reached the
    submitter and not the watcher, the watcher polled an id that was never submitted and reported
    every healthy stage job as `abandoned`. Returning the handle leaves ONE derivation site, which is
    the only form of the fix a later axis cannot re-break.

    Raises on a submit failure, which the caller maps to RETRY. Completion is the job's own registered
    commit, not something observed from here.

    ``lineage_json`` is this run's consume-layer provenance document (R26). It rides the runtime_env so
    the job writes the ``lineage`` JSONB column in the SAME commit as the data — the distributed path
    must not produce a governed dataset the in-process path would have stamped. It is provenance, never
    a credential, so echoing it back through the jobs API (which mirrors runtime_env) is harmless.

    ``from_id``/``to_id``/``run_id`` are the run's PROVENANCE IDENTITY, and are not derivable from the
    two URIs beside them: a catalog identifier (``acme-silver$features``) is what the lineage graph and
    the FGA objects are keyed by, while a storage URI is a location. The job emits its own OpenLineage
    (no Dapr sidecar on Ray pods), so without these it names its output by the URI's stem — a node no
    grant matches, which hides the run from every recipient while the job acks SUCCESS — and mints its
    own run id, which cannot MERGE onto the run the stage runner emitted for the same hop. Empty is the
    UNWIRED case and omits the variable, leaving the runner's documented stem fallback in place.
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
    # what the job's default enforces — so an un-migrated lane behaves identically. A DECLARED lane
    # can ask for a fan-out (one video into frames, one recording into speaker turns), and this is the
    # line that makes the declaration reach the job rather than being stored and ignored.
    cardinality = spec.cardinality if spec else ONE_TO_ONE

    # The work identity rides in the id: a token-less trigger used to collapse EVERY submission of a
    # stage onto `ray-<stage>-notoken`, and submit_or_reattach read the collision as success — the
    # second transform silently never ran. The same collapse hid WITH a token whenever one trigger
    # fans out to two tables of the same stage. from→to IS the transform's identity; a redelivered
    # trigger carries the same pair, so redelivery idempotency is unchanged.
    # THE PLATFORM'S HALF OF THE CONTRACT, SERIALIZED ONCE. `WorkOrder.to_env()` is documented as
    # "the ONE serialization, so no adapter hand-rolls it", and hand-rolling it here is precisely what
    # made `service_kit.lakehouse.executor` unusable against this job: the port's Ray adapter renders
    # `to_env()` into the CR's runtime_env, this submitter wrote six differently-spelled names, and the
    # two shared ZERO keys — so a job submitted through the port would have started with none of its
    # inputs bound. Measured 2026-09-07 and now pinned by
    # `tests/unit/test_the_submitter_and_the_job_agree_on_the_wire.py`.
    #
    # The floor is OMITTED when there is none rather than blanked, which `to_env` does for us: the job
    # reads a missing floor and an empty one identically ("read everything"), so the two spellings
    # agree, and omission is the one that does not assert a version that may not exist.
    order = WorkOrder(
        task=spec.task if spec else stage,
        source=WorkSource(uri=from_uri, table_id=from_id, version_floor=from_version),
        destination=WorkDestination(uri=to_uri, table_id=to_id),
        # TOKEN AND TRANSFORM RIDE THE STAMP ([[LH-159]]). Both are stamped as Ray job metadata and
        # read back — `rask.originator` recovers who a dead job was for, `rask.transform` is pinned by
        # its own test — and neither could be rendered by an adapter while they existed only as
        # arguments to this function. On the ORDER rather than on `Executor.submit` because both are
        # platform facts, and metadata handed to a submit call is rendered per adapter, which is the
        # divergence `to_env()` being the one serialization exists to prevent.
        stamp=WorkStamp(
            stage=stage,
            cardinality=cardinality,
            lineage_document=lineage_json,
            token=token or "",
            transform=spec.name if spec else "",
        ),
        identity=WorkIdentity(
            run_id=run_id,
            project=project,
            originator=originator,
            code_version=code_version,
            # WHO THE JOB REPORTS AS at the lineage ingest, and the only lineage fact that can not come
            # from the pod: one head serves all three stage lanes, and this selects which
            # `RASK_LINEAGE_TOKEN_<IDENTITY>` the job's emitter presents. Gated on the lane being
            # wired, so an unconfigured lane asserts no subject rather than a blank one.
            service_identity=settings.fga_service_identity if settings.stage_lineage_url else "",
        ),
        params=job_params,
        # THE RUN'S IDENTITY, and now also the engine's handle for it: the executor submits under this
        # key, so the in-process and Ray lanes agree on what "the same work" is by construction rather
        # than by two derivations honouring the same four axes. Derived HERE and nowhere else — a
        # caller that cannot spell the key cannot spell it differently.
        idempotency_key=derive_idempotency_key(stage=stage, token=token, from_uri=from_uri, to_uri=to_uri, code_version=code_version),
        observability=build_stage_order_observability(),
    )
    # NO CREDENTIAL AND NO ENDPOINT RIDES THIS BODY, and it is the ORDER that guarantees it rather
    # than this call site remembering to. `to_env()` is the ONE serialization and `WorkOrder` carries
    # `extra="forbid"` with no field a credential could occupy, so the guarantee is a property of the
    # TYPE. It is load-bearing because the Jobs API echoes `runtime_env` on `GET /api/jobs/<id>` — an
    # unauthenticated dashboard, proxied by compute at `/api/ray/*` and published at the edge.
    registration = TaskRegistration(task=order.task, engine=RAY_ENGINE, command=entrypoint, code_version=code_version)

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
    #
    # SUBMITTED THROUGH THE PORT, resolved BY NAME ([[LH-159]]). Naming `RayJobsApiExecutor` here would
    # swap one hard-coded engine for another and read like a fix; `executor_for` is what makes the
    # lakehouse driveable BY Ray rather than dependent ON it. `storage_options` is empty because this
    # adapter needs none — it posts to the standing cluster the pooled client already addresses, and
    # the job resolves its own credentials from the pod.
    #
    # The Ray job is now named by `order.idempotency_key` rather than `stage_submission_id(...)`. Both
    # are deterministic in the same four axes and differ only as hashes; the poller cannot drift onto
    # the other one because this function RETURNS the handle it submitted under, which is the single
    # derivation site that a later axis cannot re-break.
    handle, _outcome = await executor_for(RAY_ENGINE, storage_options={}).submit(order, registration)
    log.info(
        "ray_stage_job_submitted",
        extra={"submission_id": handle.handle, "stage": stage, "transform": spec.name if spec else "", "declared": spec is not None},
    )
    return handle.handle
