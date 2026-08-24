"""The mover's stage-transform business logic — one DAG edge, infra-free + testable.

:func:`handle_stage` is the heart of a medallion mover: given one upstream stage trigger it emits the
transform's OpenLineage event (``inputs=[from_dataset]`` -> ``outputs=[to_dataset]`` — the ``DERIVED_FROM``
edge) and publishes the next stage's trigger, so a single producer event cascades bronze->silver->gold
(R23: the producer ingests external raw straight into bronze; there is no raw tier).

The trigger is untrusted input: it is validated through ``trigger_guards.StageTrigger`` before anything
reads it, and a payload that fails is DROPped (DATA-CONTRACT §7.3 — never repaired, never raised).

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
import time
from datetime import UTC, datetime
from functools import partial
from typing import Any, NamedTuple

import httpx
from dapr.aio.clients import DaprClient
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from opentelemetry import trace

from medallion.core.best_effort import best_effort
from medallion.core.config import MedallionSettings, project_namespace
from medallion.core.metrics import record_denied, record_other_lane, record_quality_blocked, record_refused, record_stage_completion, record_transition
from medallion.schemas.events import build_run_event
from medallion.services import catalog_register, promotion_band, promotion_hold
from medallion.services import gate as gate_svc
from medallion.services.compute import existing_row_count, measure_stage, read_upstream, transform_stage
from medallion.services.derivers import UnderivableMediaError
from medallion.services.gate_decision import GateOutcome, gate_decision
from medallion.services.lane import UndeclaredLaneError, resolve_lane_async
from medallion.services.promotion import promotion_lineage
from medallion.services.trigger_guards import StageTrigger, parse_stage_trigger, uri_within
from service_kit import dapr_publish
from service_kit.governed import fga
from service_kit.lakehouse import outbox
from service_kit.lakehouse.quality import Assertion, assert_quality
from service_kit.lakehouse.warehouse_registry import (
    UnresolvableProjectError,
    is_safe_project,
    lane_key,
    project_gold_root,
    project_root,
)


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


def _dispatch_stage_workflow(
    settings: MedallionSettings,
    *,
    from_uri: str,
    to_uri: str,
    token: str | None,
    lineage_json: str,
    trigger: StageTrigger,
    event_time: str | None = None,
    pre_row_count: int | None = None,
) -> str:
    """Schedule `stage_run` for this trigger and return its instance id (S1).

    A SEAM over `DaprWorkflowClient`, the same reason ingest has one: a test must be able to assert
    that the ray branch DISPATCHES rather than measures, and it cannot stand up a sidecar to do it.

    The instance id is deterministic in the work — `stage_submission_id` already hashes `from->to`
    with the token — so a redelivered trigger re-attaches to the running instance instead of starting
    a second watcher over the same job. Dapr answers a duplicate `schedule_new_workflow` for a live
    instance with an error, which is why that is caught and reported as the re-attach it is: the first
    instance is still watching, and that is the correct outcome, not a failure to dispatch.
    """
    import dapr.ext.workflow as wf

    from medallion.services.ray_submit import stage_submission_id
    from medallion.workflow import StageJobSpec, stage_run

    stage = settings.to_namespace
    instance_id = f"stage-{stage_submission_id(stage, token, from_uri, to_uri)}"
    # R26: pass 1 OWNS the instant and hands it forward, so pass 2 reuses it rather than stamping its
    # own. Without this the in-dataset `lineage` document and the published COMPLETE disagree.
    carried = trigger.model_dump()
    if event_time is not None:
        carried["event_time"] = event_time
    # The destination's row count BEFORE the job writes — see `StageTrigger.pre_row_count`. Injected
    # like `event_time` and for the same reason: pass 2 cannot observe it, because the write it would
    # be comparing against has already happened by the time it runs.
    if pre_row_count is not None:
        carried["pre_row_count"] = pre_row_count
    spec = StageJobSpec(
        from_uri=from_uri,
        to_uri=to_uri,
        stage=stage,
        token=token,
        lineage_json=lineage_json,
        trigger=carried,
    )
    client = wf.DaprWorkflowClient()
    try:
        client.schedule_new_workflow(workflow=stage_run, input=spec.model_dump(), instance_id=instance_id)
    except Exception:
        # A schedule failure is TWO different events wearing one exception, and they need opposite
        # answers. "This instance already exists" means a watcher is already on this exact job and the
        # trigger is fully handled — acking is right. Anything else (no sidecar, state store not
        # scoped, engine down) means NOTHING is watching, and swallowing it would ack a trigger whose
        # work never starts: the job is submitted by the workflow, so no workflow means no job at all.
        #
        # So the existence of the instance is CHECKED rather than assumed. An unscoped state store is
        # the likeliest form of the second case (values.yaml scopes `medallion` for exactly this, and
        # daprd cannot hot-reload an actor state store), and it is precisely the one a blanket swallow
        # would render as a silent success on every delivery.
        if not _stage_workflow_exists(client, instance_id):
            raise
        log.info("medallion_stage_workflow_reattach", extra={"instance_id": instance_id})
    return instance_id


def _stage_workflow_exists(client: Any, instance_id: str) -> bool:
    """Whether `instance_id` names a workflow the engine knows about.

    Read through a helper so the failure to ANSWER is not read as "absent": if the state lookup itself
    raises, the engine is unreachable, which is the case that must RETRY — returning False there sends
    the caller down the re-raise path, which is the answer we want for an unreachable engine too.
    """
    try:
        return client.get_workflow_state(instance_id) is not None
    except Exception:
        return False


class StageIdentity(NamedTuple):
    """The four names one stage run reads and writes.

    A NamedTuple rather than four bare returns because every downstream use — the FGA object, the
    lineage identities, both URIs — takes them as a SET, and a caller that picks up three of four
    from a declaration and one from env produces a pair that exists in neither place.
    """

    from_namespace: str
    from_dataset: str
    to_namespace: str
    to_dataset: str


def _namespace_of(table_id: str) -> str:
    """The namespace half of a governed table id (`acme-bronze$events` -> `acme-bronze`).

    REFUSES an id with no namespace rather than falling back to the env's. A declared dataset paired
    with an undeclared namespace is exactly the silent mismatch the record exists to remove, and a
    guess here would be indistinguishable from a correct resolution at every later step.
    """
    namespace, sep, _ = table_id.partition("$")
    if not sep or not namespace:
        raise ValueError(f"table id {table_id!r} names no namespace — expected '<namespace>$<table>'")
    return namespace


def resolve_stage_identity(settings: Any, *, spec: Any, project: str) -> StageIdentity:
    """What this run reads and writes: the DECLARED record when there is one, else the env.

    The `stage_run` workflow is already parameterised by `from_uri`/`to_uri`, so this is the only
    place a mover was pinned to a single edge. With a record, a mover becomes a worker for whatever
    that record declares; without one it behaves byte-for-byte as it always did.

    Taken WHOLE, never merged: `from_id` carries its own namespace, so both halves come from the same
    source. See `_namespace_of` for why a missing namespace refuses instead of borrowing the env's.
    """
    if spec is not None:
        return StageIdentity(
            from_namespace=_namespace_of(spec.from_id),
            from_dataset=spec.from_id,
            to_namespace=_namespace_of(spec.to_id),
            to_dataset=spec.to_id,
        )
    return StageIdentity(
        from_namespace=project_namespace(project, settings.from_namespace),
        from_dataset=project_namespace(project, settings.from_dataset),
        to_namespace=project_namespace(project, settings.to_namespace),
        to_dataset=project_namespace(project, settings.to_dataset),
    )


def accepted_input_names(*, env_from_dataset: str, declared: Any | None) -> set[str]:
    """The LANE KEYS this mover accepts. One kind of thing, compared against one kind of thing.

    A stage trigger's `dataset` is a lane key: tenant-free, identical for every tenant, with the
    tenant carried separately on `trigger.project`. `settings.from_dataset` is already one. A
    `TransformSpec.from_id` is NOT -- it is a CATALOG IDENTIFIER (`acme-bronze$events`), because it
    has to resolve at `/v1/table/{id}`. Putting it in this set compared a catalog id against a lane
    key: a type error, not a spelling difference, and it made a lane declared through the door
    unreachable from the publication head.

    So the record's id is converted ONCE, by the shared `lane_key` helper that is the declared
    inverse of `project_namespace`. Identity resolution keeps using the catalog id
    (`resolve_stage_identity`), which is correct -- that is what a catalog id is for.
    """
    accepted = {env_from_dataset}
    if declared is not None:
        accepted.add(lane_key(str(getattr(declared, "project", "") or ""), str(declared.from_id)))
    return accepted


async def handle_stage(
    dapr: DaprClient,
    settings: MedallionSettings,
    event: Any,
    *,
    fga_client: OpenFgaClient | None = None,
    catalog_http: httpx.Client | None = None,
) -> dict[str, str]:
    """Handle one upstream stage trigger: emit the transform's lineage, then trigger the next stage.
    ``event`` is the untrusted Dapr CloudEvent envelope (hence ``Any``); it is parsed and shape-checked
    by ``trigger_guards.parse_stage_trigger`` before any field is read, and a payload that is not a
    valid :class:`~medallion.services.trigger_guards.StageTrigger` — a non-dict envelope, a non-string
    field, a ``token`` outside the ``Idempotency-Key`` shape — is DROPped (DATA-CONTRACT §7.3).

    When ``fga_client`` is set (MEDALLION_FGA_ENABLED), the mover first CHECKS it is authorized to produce
    the target stage — ``can_promote`` for the silver->gold mover, ``can_create_table`` for the others — as
    its own service identity. Unauthorized -> ``DROP`` (redelivery won't grant the role): the cascade
    enforces the ReBAC, so a mover lacking the validator role genuinely cannot promote to gold.

    ``dataset`` on the trigger names the lane that fired (bronze$events vs a page lane's
    bronze$pages, which share the ``medallion.bronze`` topic). A name that is not this mover's input is
    the other lane's and is DROPped; an ABSENT name makes no claim and proceeds.

    ``project`` on the trigger (#84 per-tenant routing, opt-in) resolves this stage's from/to URIs off
    the warehouse registry (``<project-root>/medallion/<namespace>``) and project-qualifies every lineage
    identity + the FGA object. FAIL CLOSED: an unsafe project or one arriving with resolution disabled
    (no ``MEDALLION_CONTROL_ROOT``) is DROPped; a project with no active warehouse records a FAIL run and
    DROPs — never a fallback to the default roots, which would transform the WRONG tenant's data while
    emitting real-looking lineage for it. No ``project`` → today's behavior, byte-identical.

    ``from_uri`` on the trigger names the upstream the catalog actually vended (I2), and is honoured
    only INSIDE the root this stage resolved — the mover opens it with its own object-store credentials,
    so an unconfined value reads whatever those credentials can reach. Outside the root → ``DROP``.
    That root is the TENANT'S WAREHOUSE for a project trigger, which is what makes I2 work: the vended
    ``<root>/<hash>_<ns>$<name>`` sits directly under it. With NO project the root is ``MEDALLION_FROM_URI``
    itself — a dataset URI, not a warehouse — so only that URI or a path beneath it can be named, and
    I2 is effectively project-only. That costs nothing today: the one publisher that sets ``from_uri``
    (``publication_trigger``) always carries the project, because the mover cannot resolve its tiers
    without it. A single-tenant deployment that ever needs a vended location must resolve a root here
    rather than widen the check."""
    # WALL-CLOCK FROM DELIVERY TO OUTCOME, measured once and used twice. `time.perf_counter` because
    # it is monotonic — a wall clock can step backwards under NTP and yield a negative duration, which
    # a histogram silently discards. The SAME value goes to `build_run_event(duration_seconds=…)` and
    # to `record_stage_completion(...)`: docs/architecture/batch-processing-invariants.md B10 requires the graph and the metric to
    # carry one number, and computing it twice at two points is how they start disagreeing.
    _t0 = time.perf_counter()
    elapsed_seconds = 0.0
    transition = f"{settings.from_namespace}->{settings.to_namespace}"
    # VALIDATE-OR-DROP, before a single field is read. Every value on this payload becomes an S3 key
    # prefix, a Lance read URI, a Ray submission id or a lineage graph value, and the BUS is a wider
    # trust surface than the token-guarded HTTP heads that also produce these triggers — so the shape
    # is checked once, here, by `trigger_guards` — the same validate-or-DROP rule the TRAINING trigger's
    # consumer already applies to its own payload (`services/train.py`) and that this handler simply had
    # no counterpart for. It lives in a module neither handler owns so the two cannot silently diverge.
    # Malformed → DROP: redelivery cannot fix deterministic garbage and a raising handler poisons the
    # subscription (DATA-CONTRACT §7.3). At WARNING because a DROP is an ack — Dapr neither redelivers
    # nor dead-letters, so an unrecorded drop makes the event simply cease to exist.
    trigger = parse_stage_trigger(event)
    if trigger is None:
        log.warning("medallion_stage_malformed", extra={"transition": transition, "event": str(event)[:200]})
        record_refused(transition, "malformed")
        return _DROP
    token = trigger.token

    # LANE DISCRIMINATION. Two ingest lanes — bronze$events and the page lane bronze$pages —
    # publish to the SAME medallion.bronze topic, so every mover subscribed to it sees both. The trigger
    # already names the dataset that was actually written (ingest_trigger._bronze_write_dataset: "the
    # trigger tells the mover which lane fired"); a name that is not THIS mover's input belongs to the
    # other lane, and running anyway transforms the wrong dataset while emitting real-looking lineage
    # attributed to the other lane's token.
    #
    # Compared against the RAW ``settings.from_dataset``, never the project-qualified ``from_dataset``
    # computed below: the trigger carries the unqualified name for every tenant, so qualifying this side
    # would drop every project trigger instead.
    #
    # Absent → no claim → proceed. The field is a discriminator, not a requirement: an external bronze
    # writer may omit it, and triggers queued before this field existed must still drain at rollout.
    arrived = trigger.dataset
    # THE DECLARED LANE IS ALSO THIS MOVER'S INPUT. The guard used to compare only against the raw
    # env `from_dataset`, so a mover pointed at a declared lane dropped its own arrivals: the head
    # published `acme-bronze$agnostic`, the env still said `bronze$events`, and the two never matched.
    # Resolved here rather than after, because a DROP decided on stale identity is indistinguishable
    # from a correct one — it acks, and the work simply ceases to exist.
    # `trigger.project` RAW, not the validated `project` below — that resolution happens after this
    # guard, and moving it earlier would change the fail-closed ordering it exists for. An absent or
    # wrong project simply finds no record, and the guard falls back to env-only: the old behaviour.
    declared_lane = await resolve_lane_async(settings, project=trigger.project or "") if settings.lane else None
    accepted = accepted_input_names(env_from_dataset=settings.from_dataset, declared=declared_lane)
    if arrived is not None and arrived not in accepted:
        # OBSERVABLE, at INFO and on a counter. A DROP is an ack: Dapr neither redelivers nor
        # dead-letters, so if the app records nothing the event simply ceases to exist. Before this
        # guard, a bronze$pages arrival drove this mover into a deterministic FAIL — and that FAIL is
        # what live-proof-2026-07-28.md used as evidence the page lane had no consumer. A silent fix
        # would have removed the symptom AND the only way to notice the lane is still unlanded.
        # (INFO is not noisy: the trigger is published once per bronze WRITE by handle_bronze_arrival,
        # not once per page — one record per ingest.)
        record_other_lane(transition)
        log.info(
            "medallion_stage_other_lane",
            extra={"transition": transition, "token": token, "arrived": arrived, "expects": settings.from_dataset},
        )
        return _DROP  # deterministic — redelivery cannot make this the right mover

    raw_project = trigger.project
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
    # WHAT THIS RUN READS AND WRITES — the declared lane record when there is one, else the env,
    # project-qualified exactly as before. This is the line that decided a mover served one edge:
    # `stage_run` has always been parameterised by from_uri/to_uri, so the pinning lived here and
    # nowhere else. Everything below — the FGA object, the lineage identities, both URIs — reads
    # these four names, so they follow the declaration automatically.
    try:
        identity = resolve_stage_identity(settings, spec=declared_lane, project=project)
    except (UndeclaredLaneError, ValueError) as exc:
        # A named-but-undeclared lane, or a declared id with no namespace. Both are DETERMINISTIC —
        # redelivery cannot make a missing record appear — so DROP rather than RETRY, and say which.
        log.warning(
            "medallion_stage_lane_unresolvable",
            extra={"transition": transition, "token": token, "project": project, "error": str(exc)},
        )
        return _DROP
    from_namespace = identity.from_namespace
    from_dataset = identity.from_dataset
    to_namespace = identity.to_namespace
    to_dataset = identity.to_dataset

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
    quality_reasons: list[str] = []
    completed = False  # set once the COMPLETE lineage emit lands — gates the FAIL-on-failure below
    # ONE instant for the whole run: the `lineage` JSONB written into the dataset (R26) and the event
    # published to the graph must name the same eventTime, or the two provenance records disagree on the
    # only field a consumer can join runs by time on.
    # REUSED when the trigger carries one (the S1 completed pass), stamped fresh otherwise. The Ray
    # lane runs this handler TWICE for one run, so a fresh stamp on the second pass is a second clock.
    event_time = trigger.event_time or datetime.now(UTC).isoformat()
    try:
        # #84: resolve THIS stage's roots for a tenant trigger — the registry read is blocking IO
        # (threadpool). No active warehouse is deterministic → the dedicated except below records the
        # FAIL run and DROPs; a transient registry outage raises IO errors into the generic RETRY path.
        from_uri, to_uri = settings.from_uri, settings.to_uri
        # The storage domain this stage is entitled to READ — the tenant's resolved warehouse root, or
        # the env-configured upstream when single-tenant. A trigger-supplied `from_uri` is confined to
        # it below; nothing else defines what this mover's credentials are allowed to open.
        read_root = settings.from_uri
        if project:
            root = await run_in_threadpool(project_root, settings.control_root, settings.storage_options(), project)
            if root is None:
                raise UnresolvableProjectError(f"project {project!r} has no active warehouse")
            read_root = root
            from_uri = f"{root}/medallion/{settings.from_namespace}"
            to_uri = f"{root}/medallion/{settings.to_namespace}"

        if project and settings.gold_warehouse_enabled:
            # Gold tier (DECISIONS "Medallion tiers"): the chart sets this env ONLY on the terminal
            # silver→gold mover, whose tenant TARGET root becomes the project's gold SERVING
            # warehouse (the serving=="gold" registry record) when one exists. The upstream READ
            # stays in the work warehouse; no gold warehouse → fall through to the work root above,
            # byte-identically. Lineage/FGA identities are untouched — only the physical root moves.
            gold_root = await run_in_threadpool(project_gold_root, settings.control_root, settings.storage_options(), project)
            if gold_root is not None:
                to_uri = f"{gold_root}/medallion/{settings.to_namespace}"

        # A trigger that NAMES the upstream wins over any path composed above. The catalog vends a
        # table's location (`s3://<warehouse>/<hash>_<ns>$<name>`); this composed
        # `{root}/medallion/{namespace}`, a path no catalog-written table has ever occupied — so the
        # cascade fired correctly, woke, and found nothing, for every ingest-written table. I2
        # ("resolve the location through the CATALOG, never compose a path") read from the consuming
        # end. Only the READ side: the mover still owns where it WRITES.
        #
        # CONFINED to `read_root`, because the name is honoured by OPENING it with this mover's own
        # object-store credentials: unbounded, the field is a read primitive for every bucket that
        # credential can reach, and the trigger arrives off a topic anything in the mesh can publish to.
        # The catalog's vended location (`<root>/<hash>_<ns>$<name>`) sits inside the same root the
        # registry resolved, so the legitimate publisher is unaffected. Outside it → DROP, never a
        # silent fall-back to the composed path: a trigger naming a source this stage may not read is
        # not a trigger to run with a different source, and substituting one would transform a dataset
        # nobody asked for under real-looking lineage.
        supplied = trigger.from_uri
        if supplied:
            if not uri_within(read_root, supplied):
                log.warning(
                    "medallion_stage_from_uri_refused",
                    extra={"transition": transition, "token": token, "project": project, "supplied": supplied[:200], "root": read_root},
                )
                # On a counter as well as the log: this is the one refusal that means someone is
                # publishing triggers this mover must not honour, and a DROP is an ack — without a
                # series there is nothing for an alert to fire on. The offending URI stays on the log
                # line; the counter carries only the closed reason vocabulary.
                record_refused(transition, "unconfined_uri")
                return _DROP
            from_uri = supplied
        # 0. Fake-Ray compute (opt-in): a REAL in-process Lance write of the downstream dataset, so the
        # emitted lineage carries the actual version + measured output statistics (rows + on-disk bytes),
        # and the cascade produces data, not just provenance. Blocking Lance/S3 IO → threadpool. Off →
        # version 1, no stats (dummy emit). A compute failure → RETRY below. With the quality gate on, the
        # mover then ASSERTS quality on the dataset it just wrote (the produced data is what's validated).
        result = None
        lineage_doc = None
        assertions: list[Assertion] = []
        if settings.compute_enabled and from_uri and to_uri:
            # Serialize the write (+ the quality read of what it just wrote) against a concurrent redelivery
            # of the same stage — single-flight so two overwrites can't race on the same target dataset.
            async with _write_lock:
                # The consume-layer provenance document (R26) is built BEFORE the write, because it is a
                # COLUMN of the table being written — a governed row must never be readable without it.
                # Its inputs carry the upstream's measured version + URI, and its DERIVED_FROM tail is
                # inherited from the upstream dataset's own `lineage` cell, so a gold row's chain reaches
                # bronze without a single graph query.
                upstream = await run_in_threadpool(read_upstream, from_uri, settings.storage_options())
                # I2, FINALLY ON THE WRITE SIDE. Ask the catalog where this table lives BEFORE
                # writing, instead of composing a path and telling it afterwards. The composed
                # `{root}/medallion/{tier}` is a layout the catalog has never vended, so the two
                # disagreed and the publish that followed opened the catalog's answer and found
                # nothing. Ordering is the whole change: registering happened AFTER the write, and
                # asking has to happen before it.
                #
                # No catalog URL is the ungoverned dev shape — it keeps its configured URI and the
                # loud warning below still fires.
                if settings.catalog_url and to_dataset:
                    to_uri = await run_in_threadpool(
                        catalog_register.ensure_stage_output,
                        catalog_url=settings.catalog_url,
                        table_id=to_dataset,
                        schema=upstream.schema,
                        delimiter=settings.delimiter,
                        token=settings.catalog_token,
                        app_token=settings.app_api_token,
                        service_identity=settings.catalog_service_identity,
                    )
                lineage_doc = promotion_lineage(
                    settings,
                    from_namespace=from_namespace,
                    from_dataset=from_dataset,
                    to_namespace=to_namespace,
                    to_dataset=to_dataset,
                    to_uri=to_uri,
                    upstream=upstream,
                    event_time=event_time,
                    token=token,
                    project=project or None,
                )
                with tracer.start_as_current_span("medallion.transform") as span:
                    span.set_attribute("lance.medallion.transition", transition)
                    span.set_attribute("lance.lineage.run_id", lineage_doc.run_id)
                    span.set_attribute("lance.lineage.chain_depth", len(lineage_doc.derived_from))
                    use_ray = settings.ray_enabled
                    if use_ray and not trigger.ray_job_done:
                        # S1 — DISPATCH, and return. This branch used to submit and then measure on the
                        # very next line, which is the defect `medallion.workflow` exists to close:
                        # `submit_stage_job` returns the instant Ray ACCEPTS the submission, so the
                        # measure opened the destination before the job had written it. When the
                        # destination survived from a prior run that measure SUCCEEDED, and the run
                        # emitted a COMPLETE stamped with a version and row count this job never
                        # produced, then fired the next tier off it — with nothing red anywhere.
                        #
                        # The waiting now lives in a workflow: it submits, polls to a terminal state on
                        # a DURABLE timer, and only on SUCCEEDED re-publishes this trigger with
                        # `ray_job_done`, which re-enters this handler through the `elif` below. The
                        # handler still acks in milliseconds, so A13's objection — a poll holding an ack
                        # across the job's runtime until the redelivery window is exhausted — does not
                        # apply to it.
                        span.set_attribute("lance.medallion.compute", "ray")
                        span.set_attribute("lance.medallion.ray_phase", "dispatched")
                        # MEASURED HERE, before the job is submitted, because this is the last moment
                        # the predecessor exists. `existing_row_count` answers None for an absent or
                        # unreadable destination, which the band reads as FIRST_PROMOTION and asks about
                        # — the same safe direction it takes everywhere else.
                        pre_rows = await run_in_threadpool(existing_row_count, to_uri, settings.storage_options())
                        instance_id = _dispatch_stage_workflow(
                            settings,
                            from_uri=from_uri,
                            to_uri=to_uri,
                            token=token,
                            lineage_json=lineage_doc.to_json(),
                            trigger=trigger,
                            event_time=event_time,
                            pre_row_count=pre_rows,
                        )
                        log.info(
                            "medallion_stage_dispatched_to_workflow",
                            extra={"transition": transition, "instance_id": instance_id, "to_uri": to_uri},
                        )
                        return _SUCCESS
                    if use_ray:
                        # The job is TERMINAL-OK — the workflow read SUCCEEDED before re-publishing — so
                        # the destination exists and measuring it is now a question about this run's
                        # output rather than a race with it.
                        span.set_attribute("lance.medallion.compute", "ray")
                        span.set_attribute("lance.medallion.ray_phase", "completed")
                        if trigger.ray_submission_id:
                            span.set_attribute("lance.medallion.ray_submission_id", trigger.ray_submission_id)
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
                            lineage=lineage_doc,
                            # DECLARE the canonical name. `to_uri` is composed from the NAMESPACE
                            # alone, while `to_dataset` is the project-qualified table id — so nothing
                            # downstream can derive one from the other (`medallion/bronze` is both
                            # `bronze$events` and `bronze$pages`). The writer is the only party holding
                            # both, so it stamps it; the maintenance sweep reads it back to emit this
                            # dataset's provenance and its FAIL events.
                            dataset_id=to_dataset,
                        )
                    # GOVERNANCE IS THE CASCADE'S, NOT ONE LANE'S. Every branch above that WROTE
                    # converges here (the dispatch branch returned before writing), so this is the
                    # one place a tier's output can be registered regardless of which compute
                    # produced it. Registration is what turns written bytes into a `table:` object;
                    # without it the dataset has no catalog record, so no FGA object, so no tuple
                    # can name it — and no grant, retention policy or deletion-protection can ever
                    # apply. Measured on the live estate 2026-08-17: `can_delete namespace:gold` =
                    # false and `describe gold$catalog` = 403, not because permission was denied but
                    # because there was no object there to hold a permission.
                    #
                    # ONE registration site, for every lane. This used to live inside a workload's
                    # own stage module, so the one workload that had it was governed and every other
                    # lane wrote ungoverned bytes — a new workload would start ungoverned BY DEFAULT
                    # and need its own bolt-on, which is backwards for an agnostic platform.
                    #
                    # Failure PROPAGATES (RegisterError): a write the catalog cannot govern must not
                    # report success, and the mover retries.
                    if to_dataset:
                        if settings.catalog_url:
                            # NOTHING LEFT TO REGISTER. `ensure_stage_output` above created the table,
                            # which registered it — and registering again is not merely redundant:
                            # `register_stage_output` resolves the location against the single
                            # hardwired MEDALLION_CATALOG_ROOT, while a vended tenant location lives in
                            # that tenant's warehouse. It would raise AFTER the Lance write committed,
                            # which is ungoverned bytes plus a retry no redelivery can clear.
                            span.set_attribute("lance.catalog.registered", to_dataset)
                        else:
                            # AN UNGOVERNED WRITE IS LOUD, NEVER SILENT. Without a catalog URL this
                            # mover cannot register, so the bytes land outside governance — which is
                            # exactly the state the estate was found in on 2026-08-17, and the reason
                            # it went unnoticed for so long is that nothing said so. It warns rather
                            # than raising because an unset URL is a DEPLOYMENT gap, not bad data:
                            # raising would turn a misconfigured chart into a permanently
                            # dead-lettering cascade, and redelivery cannot set an env var.
                            #
                            # The span attribute is the machine-readable half — alert on
                            # `lance.catalog.ungoverned` being present, and a tier writing outside
                            # governance pages someone instead of accumulating quietly.
                            log.warning(
                                "medallion_stage_output_UNGOVERNED",
                                extra={"table_id": to_dataset, "to_uri": to_uri, "missing": "MEDALLION_CATALOG_URL"},
                            )
                            span.set_attribute("lance.catalog.ungoverned", to_dataset)

                    span.set_attribute("lance.version", result.version)
                    span.set_attribute("lance.row_count", result.row_count)
                    span.set_attribute("lance.size_bytes", result.size_bytes)
                # Skipped when the CATALOG gates: running both would answer the same question twice
                # with different consequences, and the tag is the one that matters.
                if settings.quality_enabled and not settings.cascade_via_publish:
                    assertions = await run_in_threadpool(
                        assert_quality,
                        to_uri,
                        settings.storage_options(),
                        key_column=settings.quality_key_column,
                        required_columns=settings.required_column_list,
                    )
        elapsed_seconds = time.perf_counter() - _t0
        # B10: ONE duration, resolved BEFORE it is emitted. On the Ray lane this handler runs twice —
        # pass 1 submits and returns, the stage runs on the cluster for minutes-to-hours, and pass 2 is
        # the measure-and-emit wake-up. `elapsed_seconds` is pass 2's own wall time, so emitting it put
        # SECONDS in the graph for a stage that ran for hours, while the metric below already preferred
        # the watcher's measured span. The graph is the durable audit trail, so the authoritative record
        # was the wrong one and the metric that matched reality was the one treated as approximate.
        #
        # The value existed; it was simply computed after the event that needed it. Resolved here and
        # read twice, so the two cannot drift apart again.
        stage_seconds = trigger.ray_duration_seconds if (trigger.ray_job_done and trigger.ray_duration_seconds is not None) else elapsed_seconds
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
            duration_seconds=stage_seconds,
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
            originator=trigger.originator or None,
            # The run's model identity + build sha, when the transform declares them. A workload
            # that identifies its model reports it here; empty/None otherwise, which renders NO
            # facet (byte-parity holds). The cascade neither knows nor asks which workload ran.
            models=(result.models if result else None) or None,
            commit_sha=result.commit_sha if result else None,
            event_time=event_time,  # the same instant the in-dataset `lineage` document names (R26)
            # No compute ran (the chart's DEFAULT — `compute.enabled: false`), so nothing was written and
            # the event must not describe a dataset: bare output + an explicit mark. The run is still
            # recorded because the cascade and the audit trail both want the provenance shape.
            synthetic=result is None,
        )
        # 1. Emit the transform's lineage DURABLY (#4): stage the full event in the object-store outbox,
        # publish, drop on ack — so a crash between the Lance commit above and this publish can't lose it
        # (the lineage relay re-ingests any staged survivor, idempotent on run_id). Degrades to a plain
        # publish when no outbox_uri is set. Runs even on a quality failure, so the failed assertions are
        # recorded and the bad batch stays auditable.
        # Set BEFORE the emit, not after it returns. `publish_lineage_with_outbox` STAGES the COMPLETE
        # and only then publishes, re-raising on failure with the event left staged — that is the
        # crash window working. The run succeeded the moment the Lance write committed; redelivery
        # re-publishes the staged COMPLETE, idempotent on its deterministic run_id.
        #
        # This ordering was once LOAD-BEARING for a second reason, and no longer is: `stage_event`
        # keyed on run_id alone while `build_run_event` excludes event_type from it, so a COMPLETE and
        # a FAIL for one run shared `<run_id>.json` — and with the flag set after the await, a COMPLETE
        # whose publish failed left `completed = False`, the handler below staged a FAIL, and that
        # truncating write destroyed the staged COMPLETE. The staged object is keyed per EVENT now
        # (`outbox._object_key`), so the hazard is gone rather than sequenced around. The ordering
        # stays because it is still the correct one on its own terms, not because it is a workaround.
        completed = True
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
        # 2. THE BAND, evaluated BEFORE any promotion. Under `cascade_via_publish` the publish IS the
        # promotion — the catalog's tag move is what wakes the next stage — so a breach noticed after
        # that tag has moved cannot un-move it. Evaluated here and RULED ON by `gate_decision`, which
        # owns the ordering; it used to be an `elif` beneath the publish branch, where it never ran.
        band_reasons: list[str] = []
        if result is not None and promotion_hold.review_enabled(settings):
            # The comparison point comes from the WRITE, not from `version - 1`. That arithmetic is wrong
            # for this writer and was silently wrong in production: the data lands in one commit and the
            # lineage index in a SECOND, so the reported version is N+1 and `version - 1` is N — the
            # commit that already holds the new rows. Measured 2026-08-23: 8 -> 200 and then 200 -> 1000
            # rows both published without ever asking, because the delta was structurally zero.
            #
            # `None` means the writer could not observe a predecessor — a fresh destination, or the RAY
            # lane, whose job writes out-of-process and is measured only after it finished. The band
            # reads that as FIRST_PROMOTION and ASKS, which is this module's stated policy for an
            # unknown history ("a dataset we cannot read the history of gets a person's attention
            # instead of a silent promote") and the safe direction.
            #
            # The RAY lane no longer relies on that fallback. Pass 1 measures the destination in the
            # last moment the predecessor exists and hands the count forward on the trigger
            # (`StageTrigger.pre_row_count`), so that lane compares like any other and asks only when
            # a promotion is genuinely unusual — instead of on every single run.
            # The WRITER's observation first; the trigger's carried count only when the writer had
            # none. That ordering matters: in-process writes observe the predecessor directly and are
            # authoritative, while `pre_row_count` is pass 1's older reading of the same thing. Both
            # absent is still FIRST_PROMOTION, so an unreadable destination keeps asking.
            previous_rows = promotion_band.resolve_previous_row_count(observed=result.previous_row_count, carried=trigger.pre_row_count)
            # THE DECLARED GATE WINS. A project that declared one through the catalog's admin-gated
            # door governs its own band; one that declared nothing keeps the chart's settings,
            # byte-for-byte. Resolved here rather than at boot because the record is editable while
            # the pod runs — that is the whole point of the door, and reading it per-dispatch is what
            # makes a threshold change take effect without a `helm upgrade`.
            gate = gate_svc.effective_gate(settings, await gate_svc.resolve_gate_async(settings, project=project))
            band_reasons = promotion_band.review_reasons(
                row_count=result.row_count,
                previous_row_count=previous_rows,
                band=gate.review_band,
            )
        # ASK THE CATALOG'S GATE BEFORE DECIDING — but ONLY when the band would hold. The publish IS
        # the promotion on this path, so a review that runs after it has nothing left to withhold, and
        # one that runs before it cannot name what it is reviewing unless it can ask. `gate_only`
        # returns the identical assertions on the identical version with the tag untouched, which is
        # what lets a corrupt finding block with its names intact AND an unusual-but-valid promotion be
        # held for a person.
        #
        # Guarded on `band_reasons` because the probe is a FULL ASSERTION SCAN, and running it on every
        # promotion would pay for it twice on the overwhelming majority that were never going to be
        # held. With no breach there is nothing to distinguish, so the publish gates as it always did —
        # one call, byte-identical to before. The second scan is bought only where it decides
        # something: separating "unusual" from "corrupt", which is precisely the distinction the review
        # cannot make on its own.
        failed_assertions = [a.assertion for a in assertions if not a.success] if assertions else []
        if band_reasons and settings.cascade_via_publish and to_dataset and result is not None:
            verdict = await run_in_threadpool(
                partial(
                    catalog_register.publish_stage_output,
                    catalog_url=settings.catalog_url,
                    table_id=to_dataset,
                    version=result.version,
                    key_column=settings.quality_key_column,
                    required_columns=settings.required_column_list,
                    token=settings.catalog_token,
                    app_token=settings.app_api_token,
                    service_identity=settings.catalog_service_identity,
                    timeout_seconds=settings.publish_timeout_seconds,
                    gate_only=True,
                    client=catalog_http,
                )
            )
            failed_assertions = list(verdict.failed_assertions)

        decision = gate_decision(
            failed_assertions=failed_assertions,
            band_reasons=band_reasons,
            cascade_via_publish=settings.cascade_via_publish,
            has_target=bool(to_dataset) and result is not None,
            has_pub_topic=bool(settings.pub_topic),
        )
        # 3. A failed assertion BLOCKS — record it, do not trigger the next stage, so a bad batch
        # cannot cascade. Composes with the FGA gate above.
        if decision is GateOutcome.BLOCK:
            quality_blocked = True
            quality_reasons = failed_assertions
        # 3b. A band breach is a QUESTION (§9.1): unusual rather than broken, so it becomes a hold a
        # person is asked about rather than a verdict this code invents.
        elif decision is GateOutcome.HOLD:
            quality_blocked = True
            quality_reasons = band_reasons
        # 3c. PUBLISH what was written and let the CATALOG gate it — its tag move is the trigger, so
        # there is nothing else to fire. A refusal is a normal outcome that names its assertions, and
        # those become the hold a person may be asked about.
        #
        # `result is not None` restates an invariant `gate_decision` already enforces — it only returns
        # PUBLISH when `has_target`, which includes it — and is here so the checker can narrow
        # `result` for `result.version` below. A PUBLISH that somehow arrived without one falls
        # through to no branch, which is the same terminal outcome as NOTHING.
        elif decision is GateOutcome.PUBLISH and result is not None:
            outcome = await run_in_threadpool(
                catalog_register.publish_stage_output,
                catalog_url=settings.catalog_url,
                table_id=to_dataset,
                version=result.version,
                key_column=settings.quality_key_column,
                required_columns=settings.required_column_list,
                token=settings.catalog_token,
                app_token=settings.app_api_token,
                service_identity=settings.catalog_service_identity,
                timeout_seconds=settings.publish_timeout_seconds,
                client=catalog_http,
            )
            if not outcome.published:
                quality_blocked = True
                quality_reasons = outcome.failed_assertions
        # 4. Trigger the next stage (unless terminal — gold has no pub_topic — or blocked by the gate).
        elif decision is GateOutcome.TRIGGER:
            next_trigger = {
                "token": token,
                "dataset": settings.to_dataset,
                "namespace": settings.to_namespace,
            }
            if project:  # #84: PROPAGATE the tenant down the cascade; omitted (byte-identical) when unset
                next_trigger["project"] = project
            if trigger.originator:  # the human the cascade is for, carried the whole way down
                next_trigger["originator"] = trigger.originator
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
        with best_effort("project_unresolvable", transition=transition, token=token, project=project):
            fail_event = build_run_event(
                operation=settings.operation,
                author=settings.author,
                job_namespace=settings.job_namespace,
                inputs=[(from_namespace, from_dataset)],
                output_namespace=to_namespace,
                output_name=to_dataset,
                token=token,
                project=project or None,
                originator=trigger.originator or None,
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
        with best_effort("media_underivable", transition=transition, token=token):
            fail_event = build_run_event(
                operation=settings.operation,
                author=settings.author,
                job_namespace=settings.job_namespace,
                inputs=[(from_namespace, from_dataset)],
                output_namespace=to_namespace,
                output_name=to_dataset,
                token=token,
                project=project or None,
                originator=trigger.originator or None,
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
            with best_effort("stage_fail", transition=transition, token=token):
                fail_event = build_run_event(
                    operation=settings.operation,
                    author=settings.author,
                    job_namespace=settings.job_namespace,
                    inputs=[(from_namespace, from_dataset)],
                    output_namespace=to_namespace,
                    output_name=to_dataset,
                    token=token,
                    project=project or None,
                    originator=trigger.originator or None,
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
        # A18: a HOLD must be visible IN THE GRAPH, not only in a metric and a log line.
        #
        # Without this the only lineage a held batch leaves is the measured-write event emitted just
        # before the gate ran — which says the hop wrote its output and says nothing about the
        # promotion being refused. Anyone reading the graph sees a successful hop whose downstream
        # simply never fired, and the two explanations for that ("the gate held it" and "the trigger
        # was lost") are the ones an operator most needs told apart: one is data quality, the other
        # is an outage.
        #
        # A separate FAIL run rather than a mutation of the write event, because both facts are true:
        # the hop DID write, and the promotion WAS refused. Idempotent on the token-derived run id,
        # so redelivery MERGEs rather than accumulating holds. Suppressed and best-effort for the
        # same reason every other lineage emit here is (I8): a graph outage must not convert a
        # correct refusal into a retry storm.
        with best_effort("promotion_held", transition=transition, token=token):
            held_event = build_run_event(
                operation=settings.operation,
                author=settings.author,
                job_namespace=settings.job_namespace,
                inputs=[(from_namespace, from_dataset)],
                output_namespace=to_namespace,
                output_name=to_dataset,
                token=f"{token}:quality-hold",
                project=project or None,
                originator=trigger.originator or None,
                event_type="FAIL",
                error_message=f"quality gate HELD the promotion into {settings.to_dataset} — downstream was not triggered",
            )
            await outbox.publish_lineage_with_outbox(
                dapr,
                outbox_uri=settings.lineage_outbox_uri,
                storage_options=settings.storage_options(),
                run_id=held_event["run"]["runId"],
                event_json=json.dumps(held_event),
                pubsub_name=settings.pubsub,
                topic_name=settings.lineage_topic,
                timeout_seconds=settings.publish_timeout_seconds,
            )
        # S3/S4: with review on, the hold becomes a QUESTION rather than a verdict. The mover does
        # not decide which kind of hold this is — it publishes what the gate saw, and the review
        # workflow (hosted by the producer, beside the door a person can answer on) splits corrupt
        # from unusual. A publish that does not land degrades to the permanent BLOCK below, which is
        # the safe direction: the output is written and the FAIL run is emitted either way.
        if promotion_hold.review_enabled(settings):
            spec = promotion_hold.hold_spec(
                settings,
                token=token or "",
                project=project or "",
                from_namespace=from_namespace,
                from_dataset=from_dataset,
                to_namespace=to_namespace,
                to_dataset=to_dataset,
                reasons=quality_reasons,
                originator=trigger.originator or "",
                version=result.version if result else 0,
            )
            await promotion_hold.publish_hold(dapr, settings, spec)
        return _QUALITY_BLOCKED
    record_transition(transition)
    # Volume is recorded only when the compute MEASURED the write — a stage that committed nothing
    # reports its latency and no rows/bytes, rather than a zero a reader would take for a real result.
    # THE RAY LANE'S DURATION IS THE WATCHER'S, NOT THIS HANDLER'S. On the Ray path this is pass 2 —
    # the wake-up after the job went terminal — so `elapsed_seconds` here covers only the measure and
    # emit, and recording it would report a multi-hour Ray stage as a few seconds. `stage_run` measured
    # the real span from its own deterministic clock and handed it back on the trigger.
    record_stage_completion(
        transition,
        duration_seconds=stage_seconds,
        rows=result.row_count if result else None,
        size_bytes=result.size_bytes if result else None,
    )
    log.info("medallion_stage_moved", extra={"transition": transition, "token": token, "to": settings.to_dataset, "duration_seconds": round(stage_seconds, 3)})
    return _SUCCESS
