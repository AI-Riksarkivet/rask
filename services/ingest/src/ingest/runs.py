"""Run identity and status — the two contracts §3.4 says must be implemented, not merely declared.

The medallion's head declared `202 Accepted` on a fully synchronous handler and accepted an
`Idempotency-Key` that deduplicated nothing (open_ingest.md §1.2): the token only made the run id
converge, while the work re-ran in full. Both are pinned by tests here (A1, A2) so the declaration
and the semantics cannot drift apart again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field


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


def run_id_for(project: str, idempotency_key: str) -> str:
    """Derive the run id from the CALLER's key — the estate's idempotency pattern.

    Deterministic by construction: same project + same key -> same id, on any pod, after any crash.
    A token minted per attempt would leave one orphan run per retry, which is the failure the
    annotation publish saga had to solve with `pending_publish_id` (docs/OPERATORS.md §4).
    """
    return str(uuid.uuid5(RUN_NAMESPACE, f"{project}-ingest-{idempotency_key}"))


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


class WorkflowRunReader(Protocol):
    """Reads a run's live state from the workflow engine. A Protocol so the API needs no sidecar in
    tests."""

    def state(self, run_id: str) -> dict[str, object] | None: ...


def record_from_workflow_state(run_id: str, state: dict[str, object] | None) -> RunRecord | None:
    """Rebuild the accepted-time record from the workflow's own INPUT.

    THE FIX for a 404 after a pod restart. `InMemoryRunStore` is process-local, so killing the pod
    mid-run (A3) left a workflow that was still durably executing and an API that answered 404 for
    it — the run existed, kept working, and became unobservable. That is worse than losing progress:
    an operator watching a long harvest sees it vanish.

    Nothing needs to be persisted to fix it. The POST handler passes the whole RunSpec as the
    workflow's input, and Dapr stores that in the run's history, so `serialized_input` IS the
    accepted-time record. The store becomes a genuine cache — losing it costs one extra gRPC call,
    which is what its docstring always claimed.
    """
    payload = _as_mapping((state or {}).get("serialized_input"))
    if not payload:
        return None
    return RunRecord(
        run_id=run_id,
        project=str(payload.get("project") or ""),
        dataset=str(payload.get("dataset") or ""),
        kind=str(payload.get("kind") or ""),
    )


def merge_workflow_state(record: RunRecord, state: dict[str, object] | None) -> RunRecord:
    """Overlay the engine's live truth onto the accepted-time record.

    THE FIX for a run that completed in-cluster and kept reporting ACCEPTED with `units_total: 0`.
    `InMemoryRunStore` is written once, by the POST handler, and never again — no activity updates
    it, and none should: the workflow's own durable history IS the run's state, and a second writable
    copy of it is a consistency problem with no upside.

    So the store keeps only what the engine cannot know (the project, dataset and kind the caller
    asked for) and everything that CHANGES is read from the engine. That also makes the answer
    survive a pod restart and stay correct across replicas, neither of which a process-local counter
    could manage.

    A `None` state means the engine has no such instance. That is not an error: between the store
    write and the schedule call there is a genuine window where the run exists and the workflow does
    not, and returning the accepted record unchanged is the honest answer for it.
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
    committed = output.get("committed_version")
    rows = output.get("rows")

    # The DENOMINATOR, and it has two sources because a run needs it at two different times. While
    # the run is in flight only the custom status has it (the output does not exist yet); once the
    # run is terminal the output carries it permanently. Reading both is what makes "4 of 500"
    # available for the whole life of a run rather than only after it ends.
    total = output.get("units_total")
    if not isinstance(total, int):
        total = _as_mapping(state.get("serialized_custom_status")).get("units_total")

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
            "units_done": rows if isinstance(rows, int) else record.units_done,
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
