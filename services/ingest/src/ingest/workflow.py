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
      wait_for_external_event  suspends durably until this chunk drains
      (timer fallback)         so a lost signal degrades to slow, never stuck

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

**No polling** (A13). Workers wait on JetStream pull fetch (server-fulfilled); the workflow suspends
on an external event. The one timer below is the permitted per-run dead-man switch, and it re-arms
rather than spinning.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import dapr.ext.workflow as wf
from pydantic import BaseModel, Field


if TYPE_CHECKING:
    from dapr.ext.workflow import DaprWorkflowContext, WorkflowActivityContext

# A chunk that has not signalled within this long is re-checked rather than waited on forever. The
# dead-man switch, not a poll: it fires only when the drained signal was LOST, does one read, and
# re-arms. A13 permits exactly one such timer per run and this is it.
DRAIN_FALLBACK = timedelta(minutes=10)

# Retries are the activity's, not a hand-rolled loop: Dapr owns the backoff and the replay.
ACTIVITY_RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=5),
    max_number_of_attempts=4,
    backoff_coefficient=2.0,
)


class ChunkSpec(BaseModel):
    """One dispatchable slice of a run. Keys are carried by reference-able bounds, not inline."""

    run_id: str
    chunk_id: str
    keys: list[str] = Field(default_factory=list)


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


class RunOutcome(BaseModel):
    committed_version: int | None = None
    rows: int = 0
    errors: dict[str, str] = Field(default_factory=dict)
    status: str = "COMPLETE"


def ingest_run(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Parent workflow: enumerate, fan out, finalize once, emit lineage."""
    spec = RunSpec.model_validate(payload)

    yield ctx.call_activity(emit_start, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)

    chunks: list[dict[str, Any]] = yield ctx.call_activity(enumerate_chunks, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
    ctx.set_custom_status(f"dispatching {len(chunks)} chunks")

    # Fan out. when_all is fan-in: the parent suspends until every child has drained, and survives
    # its own pod dying because the history replays. This is the durable-orchestration property that
    # a hand-rolled counter had to imitate.
    results = yield wf.when_all([ctx.call_child_workflow(chunk_run, input=c) for c in chunks])

    parsed = [ChunkResult.model_validate(r) for r in results]
    fragments = [f for r in parsed for f in r.fragments]
    errors = {k: v for r in parsed for k, v in r.errors.items()}
    ctx.set_custom_status(f"finalizing {len(fragments)} fragments")

    # Exactly one commit for the whole run — D6. Nothing is visible in bronze until this returns, so
    # there is no observable partially-ingested state to reason about.
    outcome: dict[str, Any] = yield ctx.call_activity(
        finalize,
        input={"spec": spec.model_dump(), "fragments": fragments, "errors": errors},
        retry_policy=ACTIVITY_RETRY,
    )

    yield ctx.call_activity(emit_terminal, input={"spec": spec.model_dump(), "outcome": outcome}, retry_policy=ACTIVITY_RETRY)
    return outcome


def chunk_run(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Child workflow: publish this chunk's units, then suspend until they drain."""
    chunk = ChunkSpec.model_validate(payload)

    yield ctx.call_activity(publish_units, input=chunk.model_dump(), retry_policy=ACTIVITY_RETRY)

    # Suspend durably. The worker that completes the chunk's last unit raises this event with the
    # chunk's fragments — so completion is a signal, never a poll.
    drained = ctx.wait_for_external_event(f"drained-{chunk.chunk_id}")
    fallback = ctx.create_timer(DRAIN_FALLBACK)
    winner = yield wf.when_any([drained, fallback])

    if winner is drained:
        return ChunkResult.model_validate(drained.get_result()).model_dump()

    # The signal was lost, not the work. Ask storage truth once and continue — degrade to slow, not
    # to stuck, which is the whole reason the fallback exists.
    recovered: dict[str, Any] = yield ctx.call_activity(reconcile_chunk, input=chunk.model_dump(), retry_policy=ACTIVITY_RETRY)
    return recovered


# ── activities — every non-deterministic thing lives behind one of these ───────────────


def emit_start(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:
    """Lineage START, through lineage-kit's emitter. A run is visible before it does work."""
    raise NotImplementedError("wired in the lineage step of P1")


def enumerate_chunks(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the source adapter and slice it into chunk descriptors.

    An activity, not workflow code, because it does network I/O — and because enumeration itself must
    survive a pod death: as an activity its result is persisted and replayed, which is precisely the
    gap that made a half-published enumeration the one un-redeliverable failure in the queue-only
    design.
    """
    raise NotImplementedError("wired in the sources step of P1")


def publish_units(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> int:
    """Publish this chunk's units onto the JetStream work queue (nats-py, WorkQueuePolicy)."""
    raise NotImplementedError("wired in the worker step of P1")


def reconcile_chunk(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Storage truth for a chunk whose drained signal was lost — the dead-man's one read."""
    raise NotImplementedError("wired in the worker step of P1")


def finalize(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> dict[str, Any]:
    """The lander: fragments -> ONE Lance commit, registered through the catalog."""
    raise NotImplementedError("wired in the lander step of P1")


def emit_terminal(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:
    """Lineage COMPLETE or FAIL — the FAIL branch is the gap the medallion head never closed."""
    raise NotImplementedError("wired in the lineage step of P1")


WORKFLOWS = (ingest_run, chunk_run)
ACTIVITIES = (emit_start, enumerate_chunks, publish_units, reconcile_chunk, finalize, emit_terminal)


def register(runtime: wf.WorkflowRuntime) -> None:
    """Register everything with the runtime — one place, so nothing is silently unregistered."""
    for w in WORKFLOWS:
        runtime.register_workflow(w)
    for a in ACTIVITIES:
        runtime.register_activity(a)
