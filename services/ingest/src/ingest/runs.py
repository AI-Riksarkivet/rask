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


class WorkflowStarter(Protocol):
    """The seam over `DaprWorkflowClient` — so a test can assert dispatch without a sidecar."""

    async def start(self, run_id: str, payload: dict[str, object]) -> None: ...
