"""The ingest run as a Dapr Workflow.

Adopted by owner ruling 2026-08-03 (docs/DECISIONS.md "Ingest orchestration — Dapr Workflow IS
adopted"). It executes in the daprd sidecar the chart already injects, on the actor state store that
already exists — the adoption cost is a dependency, not infrastructure.

THE SHAPE, and why each piece is where it is:

    ingest_run                 (parent — one per POST /v1/ingests)
      emit_start               activity: lineage START, so a run is visible before it does anything
      enumerate_chunks         activity: source -> CHUNK descriptors, never units
      when_all(chunk_run ...)  child workflow per chunk, fanned out
      finalize                 activity: the lander commits ONE Lance version through the catalog
      emit_terminal            activity: lineage COMPLETE or FAIL

    chunk_run                  (child — one per ~1-10k units)
      publish_units            activity: units onto the JetStream work queue
      drain_chunk              activity: fetch, validate, stage a fragment, ack — the actual work
      reconcile_chunk          activity: queue truth, only when the drain reports short

**Chunks, never units.** A unit is a page image; a run is millions of them. Persisting and replaying
a million activity results would melt the state store — the plan said so, and it is the reason the
tracker looked necessary. Chunking answers it: one child workflow per ~1-10k keys returns ONE
compact result, and the workflow's own durable state becomes the ledger. That is what dissolved the
tracker (DECISIONS.md), so this file is the reason `packages/tracker` gains no consumer.

**Determinism.** Workflow functions replay from history, so every non-deterministic thing — clocks,
randomness, I/O, network — lives in an ACTIVITY. `ctx.current_utc_datetime` and `ctx.create_timer`
are the replay-safe clock; `datetime.now()` here would produce a different history on every replay
and wedge the run. This is the single easiest way to break a workflow, hence the rule stated rather
than assumed.

**No polling** (A13). The drain blocks on JetStream's pull `fetch`, which the server fulfils when
messages exist — a blocking wait, not a loop asking "is it done yet". Nothing in this plane holds an
ack across a job's runtime, which is the specific thing A13 outlaws.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import dapr.ext.workflow as wf
from pydantic import BaseModel, Field

from ingest.sizing import ResolvedSizing, resolve


if TYPE_CHECKING:
    from dapr.ext.workflow import DaprWorkflowContext, WorkflowActivityContext

# One child workflow per this many keys. Small enough that a chunk's result stays compact in the
# state store, large enough that a million-unit run does not spawn a million children. The plan's
# own figure (open_ingest.md: "child workflow per ~1-10k keys").
CHUNK_SIZE = 1000

# Retries are the activity's, not a hand-rolled loop: Dapr owns the backoff and the replay.
ACTIVITY_RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=5),
    max_number_of_attempts=4,
    backoff_coefficient=2.0,
)


class ChunkSpec(BaseModel):
    """One dispatchable slice of a run."""

    run_id: str
    chunk_id: str
    keys: list[str] = Field(default_factory=list)
    #: Resolved ONCE, by `enumerate_chunks`, and carried. It used to be re-derived at each end from
    #: env — `RASK_INGEST_ACTIVE_DATASET` or `{warehouse}/{run_id}.lance` for workers, and
    #: `{warehouse}/{project}/{dataset}.lance` for finalize. Those are different datasets: workers
    #: wrote their fragments into one and the lander committed against another, so every run would
    #: have committed an empty version while its pages sat orphaned under a run-id path. Two
    #: derivations of one location is the bug; carrying the resolved value is the fix.
    dataset_uri: str = ""

    #: The run's write partitioning, RESOLVED at accept and carried — same reason as `dataset_uri`.
    #: Re-reading env inside the drain would let a rolling restart change a live run's fragment size
    #: mid-fan-out, so two chunks of one run could write to different layouts. Defaulted rather than
    #: required so a chunk enqueued by an older build still validates.
    sizing: ResolvedSizing = Field(default_factory=resolve)


class ChunkResult(BaseModel):
    """What a drained chunk reports back — the fragments to commit, and what refused to land."""

    chunk_id: str
    fragments: list[str] = Field(default_factory=list, description="FragmentMetadata JSON blobs")
    errors: dict[str, str] = Field(default_factory=dict, description="unit key -> reason")


class RunSpec(BaseModel):
    """The parent workflow's input — the request, plus the identity minted at accept."""

    run_id: str
    kind: str
    project: str
    dataset: str
    options: dict[str, Any] = Field(default_factory=dict)
    #: Resolved at ACCEPT (`api.create_ingest`) so a refusal is a 400 rather than a drain that hangs,
    #: and so the whole fan-out shares one set of numbers.
    sizing: ResolvedSizing = Field(default_factory=resolve)


class RunOutcome(BaseModel):
    committed_version: int | None = None
    rows: int = 0
    errors: dict[str, str] = Field(default_factory=dict)
    status: str = "COMPLETE"


def ingest_run(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Parent workflow: enumerate, fan out, finalize once, emit lineage."""
    spec = RunSpec.model_validate(payload)

    yield ctx.call_activity(emit_start, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)

    # BEFORE the fan-out, not before the commit. D6's creation two-step said "created empty before
    # any fragment is COMMITTED", and that reading put it in `finalize` — which is too late, because
    # a fragment written against a dataset that does not exist yet takes lance's DEFAULTS for the
    # creation-time flags. The first in-cluster run fetched, validated and wrote every fixture and
    # then died at commit with "added files with version 2.1. However, the data storage version is
    # 2.2." The dataset must exist before the first WRITE, so the writers inherit its format.
    # The location the CATALOG vends, captured once and threaded into every chunk. Deriving it a
    # second time inside `enumerate_chunks` is what broke the first catalog-backed run: workers wrote
    # fragments to the env-composed warehouse path while the catalog's table lived somewhere else, and
    # the commit was refused with "commit references 4 data file(s) not present under the table
    # location (did the direct write target the wrong prefix?)" — after every unit had been fetched.
    # One resolution, carried; never two derivations of one location.
    dataset_location: str = yield ctx.call_activity(ensure_dataset, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)

    chunks: list[dict[str, Any]] = yield ctx.call_activity(
        enumerate_chunks,
        input={"spec": spec.model_dump(), "dataset_uri": dataset_location},
        retry_policy=ACTIVITY_RETRY,
    )
    # The enumerated total, published as CUSTOM STATUS so it is readable while the run is still
    # going. `units_total` was declared on the run record and never assigned by anything, so the API
    # could say "4 done" and never "4 of 500" — no progress bar was possible for exactly the long
    # harvest where one matters. Custom status rides the workflow's own durable state, so it survives
    # a pod death like every other run fact and needs no second writable copy.
    units_total = sum(len(chunk.get("keys") or ()) for chunk in chunks)
    ctx.set_custom_status(json.dumps({"units_total": units_total, "chunks": len(chunks)}))

    # Fan out. when_all is fan-in: the parent suspends until every child has drained, and survives
    # its own pod dying because the history replays. This is the durable-orchestration property that
    # a hand-rolled counter had to imitate.
    results = yield wf.when_all([ctx.call_child_workflow(chunk_run, input=c) for c in chunks])

    parsed = [ChunkResult.model_validate(r) for r in results]
    fragments = [f for r in parsed for f in r.fragments]
    errors = {k: v for r in parsed for k, v in r.errors.items()}
    ctx.set_custom_status(json.dumps({"units_total": units_total, "finalizing": len(fragments)}))

    # Exactly one commit for the whole run — D6. Nothing is visible in bronze until this returns, so
    # there is no observable partially-ingested state to reason about.
    outcome: dict[str, Any] = yield ctx.call_activity(
        finalize,
        input={"spec": spec.model_dump(), "fragments": fragments, "errors": errors, "units_total": units_total},
        retry_policy=ACTIVITY_RETRY,
    )

    yield ctx.call_activity(emit_terminal, input={"spec": spec.model_dump(), "outcome": outcome}, retry_policy=ACTIVITY_RETRY)
    return outcome


def chunk_run(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Child workflow: publish this chunk's units, then drain them.

    THE DRAIN IS AN ACTIVITY, and the first version's was not — it suspended on an external event
    that nothing in the estate ever raised. Read together, `worker.signal_drained` published to a
    NATS subject while this waited on a Dapr workflow event, with no bridge between them; and no
    Deployment ran a `Worker` at all. So every chunk would have published its units, waited the full
    ten-minute fallback, reconciled an untouched queue, and reported zero fragments. The lane looked
    complete because a run with no units never reached this code.

    Draining inside an activity is what makes the queue real rather than decorative: Dapr persists
    the activity's result and replays the activity — not the workflow's decisions — if the pod dies,
    while JetStream still owns redelivery, poison parking and `max_ack_pending` backpressure within
    the chunk. Fan-out is across CHUNKS (Dapr distributes child workflows over the pod fleet) and,
    within a chunk, across `sizing.fetch_concurrency`.

    Nothing here polls (A13): the activity blocks on JetStream's server-fulfilled pull fetch.
    """
    chunk = ChunkSpec.model_validate(payload)

    yield ctx.call_activity(publish_units, input=chunk.model_dump(), retry_policy=ACTIVITY_RETRY)

    drained: dict[str, Any] = yield ctx.call_activity(drain_chunk, input=chunk.model_dump(), retry_policy=ACTIVITY_RETRY)

    # Confirm against the QUEUE, not against the drain's own report. A drain that returned early —
    # its fetch timed out because another pod held the units — is indistinguishable from a complete
    # one in its own result, and only the stream knows the difference. `num_pending == 0` on a
    # WORK_QUEUE stream means every unit was acked, by whichever worker did it.
    if drained.get("errors") or drained.get("units_done", 0) < len(chunk.keys):
        reconciled: dict[str, Any] = yield ctx.call_activity(reconcile_chunk, input=chunk.model_dump(), retry_policy=ACTIVITY_RETRY)
        drained = {**drained, "errors": {**drained.get("errors", {}), **reconciled.get("errors", {})}}

    return ChunkResult.model_validate({k: v for k, v in drained.items() if k in ChunkResult.model_fields}).model_dump()


# ── activities — every non-deterministic thing lives behind one of these ───────────────


def emit_start(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:
    """Lineage START, through lineage-kit. A run is visible in the graph before it does any work.

    Deliberately first: the medallion emitted only on COMPLETE, so a run that died mid-harvest left
    NO record at all — not a failed one, none. A START here means a crashed run is a visibly
    incomplete run rather than an absence someone has to notice.
    """
    spec = RunSpec.model_validate(payload)
    _lineage().start(spec.run_id, spec.project, spec.dataset, spec.kind, spec.options)


def ensure_dataset(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> str:
    """Create the run's bronze dataset EMPTY, carrying the creation-time flags. D6 step 1.

    Idempotent, so a replay is a no-op rather than a second create — which matters because this runs
    before the fan-out and is therefore the activity most likely to be replayed.
    """
    from ingest.runtime import ensure_dataset_at

    return ensure_dataset_at(RunSpec.model_validate(payload))


def enumerate_chunks(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the source adapter and slice it into chunk descriptors.

    An activity, not workflow code, because it does network I/O — and because enumeration itself must
    survive a pod death: as an activity its result is persisted and replayed, which is precisely the
    gap that made a half-published enumeration the one un-redeliverable failure in the queue-only
    design.

    Returns CHUNKS, never units. One child workflow per CHUNK_SIZE keys returns one compact result;
    a million activity results would melt the state store, which is the whole reason chunking exists
    and the reason this plane needs no separate ledger.
    """
    from ingest.sources import SourceSpec, build_source, iter_unit_keys

    spec = RunSpec.model_validate(payload["spec"])
    source_spec = SourceSpec(kind=spec.kind, project=spec.project, dataset=spec.dataset, options=spec.options)
    # KEYS, not objects. `iter_units` reads every object's bytes to hand back its uri — so
    # enumerating a IIIF volume through it downloaded the whole volume here and the workers then
    # downloaded it again. Two full transfers of the source, the first with no backpressure at all.
    keys = list(iter_unit_keys(build_source(source_spec)))

    uri = str(payload["dataset_uri"])
    chunks: list[dict[str, Any]] = []
    for index in range(0, len(keys), CHUNK_SIZE):
        window = keys[index : index + CHUNK_SIZE]
        chunks.append(
            ChunkSpec(
                run_id=spec.run_id,
                chunk_id=f"{spec.run_id}-c{index // CHUNK_SIZE}",
                keys=window,
                dataset_uri=uri,
                # Carried, not re-resolved — same reason as `dataset_uri` above.
                sizing=spec.sizing,
            ).model_dump()
        )
    return chunks


def drain_chunk(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Consume this chunk's units: fetch, validate, stage a fragment, ack. The plane's actual work.

    Safe to retry, which is the only reason it can be an activity at all. Every unit acked by a
    previous attempt staged its fragment's identity next to its bytes BEFORE acking
    (`ingest.staging`), so a replay after a pod death re-drains only what is still on the queue and
    `finalize` recovers the rest from storage. Without that staging the retry would be silent data
    loss: acked units are gone from a WORK_QUEUE stream, and their fragments' names died with the
    pod.
    """
    from ingest.runtime import drain_chunk_units

    chunk = ChunkSpec.model_validate(payload)
    return _run_async(drain_chunk_units(chunk))


def publish_units(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> int:
    """Publish this chunk's units onto the JetStream work queue.

    Idempotent by construction under replay: JetStream dedupes on the message id within the stream's
    duplicate window, and a unit's id is derived from (run, key) — both stable. A replayed activity
    therefore re-publishes without re-queuing work, which matters because Dapr replays an activity
    whose result was not durably recorded before the pod died.
    """
    from ingest.runtime import publish_chunk_units

    chunk = ChunkSpec.model_validate(payload)
    return _run_async(publish_chunk_units(chunk))


def reconcile_chunk(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Storage truth for a chunk whose drained signal was lost — the dead-man's one read.

    Asks the QUEUE what is outstanding rather than a ledger: with WORK_QUEUE retention an acked unit
    is gone, so `num_pending == 0` means the chunk really did drain and the signal was simply lost.
    Degrades to slow, never to stuck.
    """
    from ingest.runtime import reconcile_from_queue

    chunk = ChunkSpec.model_validate(payload)
    return _run_async(reconcile_from_queue(chunk))


def finalize(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> dict[str, Any]:
    """The lander: fragments -> ONE Lance commit, registered through the catalog."""
    from ingest.runtime import finalize_run

    spec = RunSpec.model_validate(payload["spec"])
    outcome = finalize_run(spec, payload.get("fragments") or [], payload.get("errors") or {})
    # Carried into the terminal output so a FINISHED run still reports what it set out to do — the
    # custom status is the live view, this is the permanent one.
    outcome["units_total"] = int(payload.get("units_total") or 0)
    return outcome


def emit_terminal(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:
    """Lineage COMPLETE or FAIL — the FAIL branch is the gap the medallion head never closed.

    A run that fails must leave a FAIL record, not silence. The medallion turned a ValueError into a
    400 and emitted nothing, so a failed harvest was indistinguishable from one that never started.
    """
    spec = RunSpec.model_validate(payload["spec"])
    outcome = RunOutcome.model_validate(payload["outcome"])
    _lineage().terminal(
        spec.run_id,
        outcome.status,
        outcome.committed_version,
        outcome.rows,
        outcome.errors,
        project=spec.project,
        dataset=spec.dataset,
    )


def _lineage() -> Any:  # noqa: ANN401 — resolved lazily so importing this module needs no emitter
    from ingest.runtime import lineage_emitter

    return lineage_emitter()


def _run_async(coro: Any) -> Any:  # noqa: ANN401
    """Run a coroutine from a SYNC activity body.

    Dapr Workflow activities are sync callables, but the queue client is async — `nats-py` has no
    sync surface. A fresh loop per activity is correct here rather than wasteful: an activity is a
    short, isolated unit that Dapr may replay on any worker thread, so there is no long-lived loop to
    attach to and nothing to keep warm between invocations.
    """
    import asyncio

    return asyncio.run(coro)


WORKFLOWS = (ingest_run, chunk_run)
ACTIVITIES = (emit_start, ensure_dataset, enumerate_chunks, publish_units, drain_chunk, reconcile_chunk, finalize, emit_terminal)


def register(runtime: wf.WorkflowRuntime) -> None:
    """Register everything with the runtime — one place, so nothing is silently unregistered."""
    for w in WORKFLOWS:
        runtime.register_workflow(w)
    for a in ACTIVITIES:
        runtime.register_activity(a)
