"""Run identity and status — the two contracts §3.4 says must be implemented, not merely declared.

The medallion's head declared `202 Accepted` on a fully synchronous handler and accepted an
`Idempotency-Key` that deduplicated nothing (open_ingest.md §1.2): the token only made the run id
converge, while the work re-ran in full. Both are pinned by tests here (A1, A2) so the declaration
and the semantics cannot drift apart again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, Field


if TYPE_CHECKING:
    # `Mapping`, not `dict`, on every parameter below that only READS the engine's state. `dict` is
    # INVARIANT in its value type, so `dict[str, str]` — what a state payload written out as a
    # literal infers as — is not assignable to `dict[str, object]`, and the callers that hand one
    # over are correct. Nothing here mutates the state, so the read-only supertype is both the
    # honest signature and the one those callers already satisfy.
    from collections.abc import Mapping


# The namespace that makes run ids deterministic across processes and restarts. Fixed constant, not
# uuid1/uuid4: the whole point is that the SAME key yields the SAME run id on a different pod.
RUN_NAMESPACE = uuid.UUID("6f5c1f2e-9a3d-4a1e-8b77-2f0f1d9c4a10")

RunStatus = Literal["ACCEPTED", "RUNNING", "COMPLETE", "COMPLETE_WITH_ERRORS", "FAILED"]

# The scheduling call is a blocking gRPC round-trip to the sidecar. Bounded, because A1 is a
# CONTRACT: 202 in under a second. Left unbounded it hangs the request — observed in-cluster at 15s
# and climbing, which turns the estate's flagship async guarantee into a worse experience than the
# synchronous handler it replaced.
#
# Lives HERE rather than in the package __init__ so both the app factory and the API router can read
# it: api.py importing it from `ingest` is a cycle (the package imports the router), and the error it
# raises is an ImportError at collection time in every test, which is a lot of noise for one number.
SCHEDULE_TIMEOUT_SECONDS = 3.0


class ScheduleUnavailable(Exception):
    """The engine could not be ASKED to take the run — retryable, and the same answer as a timeout.

    The bounded schedule call raises `TimeoutError` when the sidecar is merely slow, and for a while
    that was the ONLY failure the door mapped. Every other sidecar failure is the same condition
    wearing a different exception — daprd not up yet, a refused gRPC channel, "the state store is not
    configured to use the actor runtime" — and each of those escaped as a raw 500, which tells a
    client its request can never work when the truth is "ask again".

    Raised only by the transport adapter (`_DaprWorkflowStarter`), never by workflow code: a
    programming error on this side is not retryable and must keep its 500 rather than becoming a
    503 the caller loops on forever.
    """


def run_id_for(project: str, idempotency_key: str) -> str:
    """Derive the run id from the CALLER's key — the estate's idempotency pattern.

    Deterministic by construction: same project + same key -> same id, on any pod, after any crash.
    A token minted per attempt would leave one orphan run per retry, which is the failure the
    annotation publish saga had to solve with `pending_publish_id` (docs/OPERATORS.md §4).

    INJECTIVE, and a printable separator cannot make it so. `f"{project}-ingest-{key}"` lets the
    delimiter occur inside either field, so distinct pairs render one string: project `ra` with
    `Idempotency-Key: batch-ingest-7` and project `ra-ingest-batch` with key `7` both give
    `ra-ingest-batch-ingest-7`. `PROJECT_PATTERN` permits `-` (`warehouse_registry.py:37`) and
    `IngestRequest.project` carries no pattern at all, while the key half is a caller-supplied HTTP
    header — so this is reachable, and it is CROSS-TENANT: the id is the workflow instance id AND the
    dedupe key, so one caller's POST is answered `deduplicated: true` against another project's run
    and their `GET /v1/ingests/{id}` resolves to it.
    """
    # `\x00` because it cannot occur in either field — an HTTP header value cannot carry a NUL, and a
    # project name is validated against a pattern that admits no control characters. That is a
    # property of the alphabet rather than a convention about what people name things, which is the
    # only kind of separator argument that survives a hostile input.
    return str(uuid.uuid5(RUN_NAMESPACE, "\x00".join((project, "ingest", idempotency_key))))


class RunRecord(BaseModel):
    """What `GET /v1/ingests/{id}` returns. Progress is derived, never a stored counter."""

    run_id: str
    project: str
    dataset: str
    kind: str
    status: RunStatus = "ACCEPTED"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    units_total: int = 0
    units_done: int = 0
    errors: dict[str, str] = Field(default_factory=dict)
    committed_version: int | None = None
    lineage_run_present: bool = False

    # The DISPATCH half, and the reason the dedupe branch can be trusted. `scheduled` is set only
    # once the engine has ACCEPTED the workflow, so a stored record with `scheduled=False` is a run
    # with nothing driving it — re-drivable, not a duplicate. Without the distinction, storing the
    # record before the dispatch (so a concurrent GET can see the run) made mere existence read as
    # "already started": the 503's own advice — retry with the same Idempotency-Key — then resolved
    # to `deduplicated=true` forever and the run became a permanent zombie, a status with no executor.
    #
    # `dispatch_started_at` is the LEASE over the window in which a repeat POST must not re-drive.
    # The schedule call is capped at SCHEDULE_TIMEOUT_SECONDS, so an attempt older than that cap is
    # over however its request ended — including a client disconnect, which unwinds the handler
    # without running any except-branch and would otherwise strand the claim.
    scheduled: bool = False
    dispatch_started_at: datetime | None = None

    # The PUBLICATION half (§ D2). A commit makes rows readable; only a publication makes them ready,
    # so a run that committed and did not publish is a distinct, visible state rather than a green
    # run with a silent hole. `from_version`/`to_version` are the range a consumer resolves its delta
    # from (D-R3) and belong on the record for the same reason the committed version does.
    published: bool | None = None
    from_version: int | None = None
    to_version: int | None = None
    publish_reason: str | None = None
    publish_error: str | None = None

    @property
    def is_defective(self) -> bool:
        """A8: 'green sync with no lineage edge is a bug the UI should surface'.

        A run that reports COMPLETE while the lineage graph has no run for it is not success — it is
        a hole in the record. Surfacing it as a defect state is the difference between an estate that
        knows its provenance and one that merely believes it.
        """
        return self.status in ("COMPLETE", "COMPLETE_WITH_ERRORS") and not self.lineage_run_present


def is_redrivable(record: RunRecord) -> bool:
    """True when a stored run has no dispatch behind it, so a repeat POST must SCHEDULE it.

    The predicate the dedupe branch turns on, and the whole of the zombie fix. Three states, and only
    the middle one is a genuine duplicate:

    * `scheduled` — the engine took it. A repeat starts nothing (A2), whatever its status now is.
    * a fresh `dispatch_started_at` — an attempt is in flight on some request right now. Also starts
      nothing: the schedule call is bounded, so waiting the bound out is cheaper than racing two
      dispatches of the same instance id to the engine.
    * neither — nobody is driving this run. The retry the 503 advises has to be able to dispatch it,
      or the advice is a guarantee of the bug it claims to recover from.

    The lease is time-based rather than cleared in a `finally` on purpose: a disconnected client
    unwinds the handler with `CancelledError`, which no except-branch here catches, and an in-flight
    marker that only a normal return can clear would strand exactly the run this predicate exists to
    free.
    """
    if record.scheduled:
        return False
    started = record.dispatch_started_at
    if started is None:
        return True
    return (datetime.now(UTC) - started).total_seconds() >= SCHEDULE_TIMEOUT_SECONDS


class RunStore(Protocol):
    """Where accepted runs live. A Protocol so the API is testable without a live Dapr sidecar."""

    async def get(self, run_id: str) -> RunRecord | None: ...

    async def put(self, record: RunRecord) -> None: ...

    async def recent(self, limit: int) -> list[RunRecord]: ...


class InMemoryRunStore:
    """The default store. Deliberately NOT durable — run truth is the workflow's, not this cache.

    The workflow owns run state (docs/DECISIONS.md: Dapr Workflow IS adopted); this is a read-side
    index so `GET /v1/ingests/{id}` can answer without a workflow query on every poll. Losing it
    costs a re-read, never correctness.
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}

    async def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    async def put(self, record: RunRecord) -> None:
        self._runs[record.run_id] = record

    async def recent(self, limit: int) -> list[RunRecord]:
        """The runs THIS REPLICA accepted, newest first.

        A read-side index, and the endpoint says so rather than implying otherwise. Runs accepted by
        another replica are absent, and a restart empties it — which is correct for what this is
        (`get` exists to answer a poll without a workflow query) and would be a lie if it were
        presented as the estate's run list. The durable list is the LINEAGE graph, which is why the
        compute zone's runs page reads that.
        """
        ordered = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
        return ordered[: max(0, limit)]


class WorkflowStarter(Protocol):
    """The seam over `DaprWorkflowClient` — so a test can assert dispatch without a sidecar."""

    async def start(self, run_id: str, payload: dict[str, object]) -> None: ...


#: Dapr's runtime statuses mapped onto the estate's vocabulary. `SUSPENDED` reads as RUNNING on
#: purpose: a paused run is not a finished one, and giving it a terminal-looking status would invite
#: an operator to move on from a run that is still there.
_RUNTIME_STATUS: dict[str, RunStatus] = {
    "PENDING": "ACCEPTED",
    "RUNNING": "RUNNING",
    "SUSPENDED": "RUNNING",
    "COMPLETED": "COMPLETE",
    "FAILED": "FAILED",
    "TERMINATED": "FAILED",
}


class WorkflowTerminator(Protocol):
    """Stops a live run. A Protocol for the same reason the two beside it are: the route must be
    exercisable without a sidecar, or the one door that stops a runaway harvest is the door nothing
    tests."""

    def terminate(self, run_id: str) -> bool: ...


class WorkflowRunReader(Protocol):
    """Reads a run's live state from the workflow engine. A Protocol so the API needs no sidecar in
    tests."""

    def state(self, run_id: str) -> Mapping[str, object] | None: ...


def record_from_workflow_state(run_id: str, state: Mapping[str, object] | None) -> RunRecord | None:
    """Rebuild the accepted-time record from the workflow's own INPUT.

    THE FIX for a 404 after a pod restart. `InMemoryRunStore` is process-local, so killing the pod
    mid-run (A3) left a workflow that was still durably executing and an API that answered 404 for
    it — the run existed, kept working, and became unobservable. That is worse than losing progress:
    an operator watching a long harvest sees it vanish.

    Nothing needs to be persisted to fix it. The POST handler passes the whole RunSpec as the
    workflow's input, and Dapr stores that in the run's history, so `serialized_input` IS the
    accepted-time record. The store becomes a genuine cache — losing it costs one extra gRPC call,
    which is what its docstring always claimed.

    `scheduled=True` is not an assumption: the state being rebuilt FROM is the engine's own record of
    the instance, which is the only evidence the door ever has that a dispatch landed. Defaulting it
    to False would make a re-POST after a pod restart look re-drivable and dispatch the run a second
    time — the mirror image of the zombie, and the worse direction of the two.
    """
    payload = _as_mapping((state or {}).get("serialized_input"))
    if not payload:
        return None
    return RunRecord(
        run_id=run_id,
        project=str(payload.get("project") or ""),
        dataset=str(payload.get("dataset") or ""),
        kind=str(payload.get("kind") or ""),
        scheduled=True,
    )


def _failure_detail(state: Mapping[str, object]) -> str:
    """The engine's own reason for a RAISED workflow failure, or "" when it records none.

    Never raises and never assumes a shape: the field has moved across dapr-ext-workflow versions and
    is not a contract this plane owns. A caller asking why a run failed is worse served by a KeyError
    than by an honest empty string.
    """
    for key in ("failure_details", "serialized_error", "error", "failure"):
        raw = state.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:500]
        detail = _as_mapping(raw)
        for field in ("message", "error_message", "stack_trace", "error_type"):
            value = detail.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return ""


def merge_workflow_state(record: RunRecord, state: Mapping[str, object] | None) -> RunRecord:
    """Overlay the engine's live truth onto the accepted-time record.

    THE FIX for a run that completed in-cluster and kept reporting ACCEPTED with `units_total: 0`.
    `InMemoryRunStore` is written by the ACCEPT path alone — the claim, then the outcome of the
    schedule call — and by nothing that EXECUTES the run: no activity updates it, and none should,
    because the workflow's own durable history IS the run's state and a second writable copy of it is
    a consistency problem with no upside.

    So the store keeps only what the engine cannot know (the project, dataset and kind the caller
    asked for) and everything that CHANGES is read from the engine. That also makes the answer
    survive a pod restart and stay correct across replicas, neither of which a process-local counter
    could manage.

    A `None` state means the engine has no such instance. That is not an error: between the store
    write and the schedule call there is a genuine window where the run exists and the workflow does
    not, and returning the accepted record unchanged is the honest answer for it. The record itself
    says which side of that window it is on — `scheduled` is what a repeat POST reads to decide
    whether to dispatch, precisely because this function cannot tell "not yet scheduled" from
    "scheduled, engine unreachable".
    """
    if state is None:
        return record

    status = _RUNTIME_STATUS.get(str(state.get("runtime_status") or ""), record.status)
    output = _as_mapping(state.get("serialized_output") or state.get("output"))

    # The workflow's own terminal state is finer-grained than the engine's: a run that landed 9,997
    # of 10,000 pages is COMPLETED as far as Dapr is concerned, and only the outcome knows it did so
    # with errors. Prefer the outcome's status wherever it produced one.
    #
    # THE ALLOWED SET INCLUDES "FAILED", and leaving it out was a latent defect that the run DEADLINE
    # made reachable. A workflow can fail BY POLICY — return a FAILED outcome rather than raising —
    # and the timeout path does exactly that: it refuses to commit a partial harvest, records the
    # reason, and returns. To Dapr that is a workflow which RETURNED, so `runtime_status` is
    # COMPLETED; with FAILED excluded here the door then reported a run that deliberately failed as
    # COMPLETE, which is the worst possible direction for this error to point.
    #
    # Gated on `status == "COMPLETE"` still, so this can only ever REFINE a normal return. An engine
    # FAILED or TERMINATED — the workflow crashed, or an operator killed it — stays authoritative and
    # cannot be overwritten by whatever happens to be in a partial output payload.
    outcome_status = output.get("status")
    if status == "COMPLETE" and outcome_status in ("COMPLETE", "COMPLETE_WITH_ERRORS", "FAILED"):
        status = outcome_status  # type: ignore[assignment]

    errors = output.get("errors")

    # A RAISED failure carries its reason somewhere else entirely, and reading only the output made
    # those runs unexplainable. Measured 2026-08-06: two runs answered
    # `{"status":"FAILED","units_total":0,"errors":{}}` — no reason, nothing to act on, and no way to
    # tell a source that enumerated nothing from an activity that died on its first call.
    #
    # The two failure MODES are not symmetric. A workflow that fails BY POLICY returns a FAILED
    # outcome and its errors ride `serialized_output` (the deadline and unit-ceiling paths do this).
    # A workflow that RAISES — an activity exhausting its retries — produces NO output at all, and
    # Dapr records the detail in its own failure field. Reading one and not the other means the
    # louder failure is the silent one.
    #
    # Several key spellings are tried because the shape has moved across dapr-ext-workflow versions
    # and the field is not part of a contract this plane controls. Failing to FIND it must not itself
    # raise — the caller is asking why a run failed, and a KeyError is a worse answer than "unknown".
    if not errors and status == "FAILED":
        detail = _failure_detail(state)
        if detail:
            errors = {"run": detail}

    committed = output.get("committed_version")

    # The DENOMINATOR, and it has two sources because a run needs it at two different times. While
    # the run is in flight only the custom status has it (the output does not exist yet); once the
    # run is terminal the output carries it permanently. Reading both is what makes "4 of 500"
    # available for the whole life of a run rather than only after it ends.
    total = output.get("units_total")
    if not isinstance(total, int):
        total = _as_mapping(state.get("serialized_custom_status")).get("units_total")

    # The NUMERATOR, and it needs the SAME two sources for the same reason. `rows` exists only once
    # `finalize` has returned, so reading it alone pinned `units_done` at 0 for the entire run — the two
    # halves of one progress bar disagreeing about when they are readable. The fan-in publishes the
    # aggregate its children reported, so a run says "320 of 500" while it is still going.
    done = output.get("rows")
    if not isinstance(done, int):
        done = _as_mapping(state.get("serialized_custom_status")).get("units_done")

    # The PUBLICATION half, carried from the workflow output the same way the commit is. Read
    # permissively (`in output` rather than a truthiness test) because `published=False` is a REAL
    # answer — the gate refused — and treating it as absent would report a blocked batch as an
    # unpublished-but-unexplained one, which is the distinction § D2 exists to make visible.
    publication = {key: output[key] for key in ("published", "from_version", "to_version", "publish_reason", "publish_error") if key in output}

    return record.model_copy(
        update={
            "status": status,
            "errors": errors if isinstance(errors, dict) else record.errors,
            "committed_version": committed if isinstance(committed, int) else record.committed_version,
            "units_done": done if isinstance(done, int) else record.units_done,
            "units_total": total if isinstance(total, int) else record.units_total,
            **publication,
        }
    )


def _as_mapping(raw: object) -> dict[str, object]:
    """A workflow's output, however the engine hands it back.

    Dapr's `WorkflowState` carries the output as a JSON STRING (`serialized_output`), while the
    in-process test doubles hand back the dict the workflow returned. Accepting both keeps one code
    path under test and in production — the alternative is a branch that only ever executes in one
    of the two, which is how a serialization mismatch reaches a live run.
    """
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}
