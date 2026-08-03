"""The ingest control API — `POST /v1/ingests`, `GET /v1/ingests/{run_id}`.

Two things here are semantics, not decoration, because their absence is what §3.4 calls the estate's
recurring disease:

* **202 means 202.** The handler mints identity, dispatches the workflow, and returns. Enumeration
  and fetching happen after the connection closes. The medallion's head declared 202 and then held
  the request through a sequential per-page harvest.
* **`Idempotency-Key` dedupes the WORK.** A repeat resolves to the same run resource and starts no
  second workflow. The medallion accepted the header and re-harvested the whole volume anyway.

Both are pinned by A1/A2 in `tests/test_ingest_api.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ingest.runs import RunRecord, RunStore, WorkflowRunReader, WorkflowStarter, merge_workflow_state, run_id_for
from ingest.sources import SourceSpec, registered_kinds


router = APIRouter(tags=["ingest"])


class IngestRequest(BaseModel):
    """A source-agnostic request — I1/I2: no source-specific route, no dataset path."""

    kind: str = Field(description="registered source kind, e.g. 'iiif' | 's3-prefix' | 'local-dir'")
    project: str
    dataset: str
    options: dict[str, object] = Field(default_factory=dict)


class IngestAccepted(BaseModel):
    """The 202 body. A run HANDLE — deliberately not a result, because there isn't one yet."""

    run_id: str
    status: str = "ACCEPTED"
    deduplicated: bool = Field(
        default=False,
        description="true when an Idempotency-Key resolved to an existing run and no work was started",
    )


def get_store(request: Request) -> RunStore:
    return request.app.state.run_store


def get_starter(request: Request) -> WorkflowStarter:
    return request.app.state.workflow_starter


def get_reader(request: Request) -> WorkflowRunReader | None:
    """The engine reader, or None where there is no sidecar to ask.

    Optional rather than required so `GET` degrades to the accepted record instead of 500ing when
    daprd is not up — the status endpoint is what an operator reaches for when something is wrong,
    and it failing precisely then would be the worst possible time.
    """
    return getattr(request.app.state, "workflow_reader", None)


@router.post("/ingests", status_code=status.HTTP_202_ACCEPTED, response_model=IngestAccepted)
async def create_ingest(
    body: IngestRequest,
    response: Response,
    store: Annotated[RunStore, Depends(get_store)],
    starter: Annotated[WorkflowStarter, Depends(get_starter)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> IngestAccepted:
    if body.kind not in registered_kinds():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown source kind {body.kind!r} — registered: {registered_kinds() or '<none>'}",
        )

    # A token-less call gets a fresh run: without a caller key there is nothing to converge ON, and
    # inventing one would make every retry a new run while pretending otherwise.
    key = idempotency_key or uuid.uuid4().hex
    run_id = run_id_for(body.project, key)

    existing = await store.get(run_id)
    if existing is not None:
        # THE dedupe. Same key + same spec resolves to the same resource and starts NO second
        # workflow — A2 asserts zero new dispatches, not merely a matching id.
        response.headers["Location"] = f"/v1/ingests/{run_id}"
        return IngestAccepted(run_id=run_id, status=existing.status, deduplicated=True)

    record = RunRecord(run_id=run_id, project=body.project, dataset=body.dataset, kind=body.kind)
    await store.put(record)

    spec = SourceSpec(kind=body.kind, project=body.project, dataset=body.dataset, options=body.options)
    await starter.start(run_id, {"run_id": run_id, **spec.model_dump()})

    response.headers["Location"] = f"/v1/ingests/{run_id}"
    return IngestAccepted(run_id=run_id)


class RunStatusResponse(BaseModel):
    """Run status, joined with the lineage record so a provenance hole is visible (A8)."""

    run_id: str
    status: str
    units_total: int
    units_done: int
    errors: dict[str, str]
    committed_version: int | None
    defect: str | None = Field(
        default=None,
        description="set when the run reports success but its lineage run is missing",
    )


@router.get("/ingests/{run_id}", response_model=RunStatusResponse)
async def get_ingest(
    run_id: str,
    request: Request,
    store: Annotated[RunStore, Depends(get_store)],
    reader: Annotated[WorkflowRunReader | None, Depends(get_reader)],
) -> RunStatusResponse:
    record = await store.get(run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no ingest run {run_id!r}")

    # The store holds only what the caller asked for; everything that MOVES is read from the engine.
    # Without this a completed run reported ACCEPTED forever — nothing writes the record a second
    # time, and nothing should, because the workflow's durable history already is the run's state.
    # `to_thread` because the Dapr client is synchronous gRPC: called inline it blocks the event loop
    # for every other request on the pod, which is the same defect the POST path had to fix.
    if reader is not None:
        record = merge_workflow_state(record, await asyncio.to_thread(reader.state, run_id))

    # A8's other half. `is_defective` was computed from a flag nothing ever set, so it fired on every
    # completed run — and a gate that always fires is a gate nobody reads. `None` means the graph
    # could not be asked, and an unanswerable question must not be reported as a defect.
    # `getattr`, not `state.__dict__.get`: Starlette's State keeps its attributes in an inner
    # `_state` dict, so `__dict__` holds only that wrapper and the lookup silently returns None —
    # which reads as "no reader configured" and would have reported a provenance defect on every
    # completed run, i.e. the exact bug this join exists to fix.
    provenance = getattr(request.app.state, "provenance_reader", None)
    if provenance is not None and record.status in ("COMPLETE", "COMPLETE_WITH_ERRORS"):
        present = await asyncio.to_thread(provenance.has_run, run_id)
        record = record.model_copy(update={"lineage_run_present": present is not False})

    return RunStatusResponse(
        run_id=record.run_id,
        status=record.status,
        units_total=record.units_total,
        units_done=record.units_done,
        errors=record.errors,
        committed_version=record.committed_version,
        defect=("run reports success but no lineage run exists for it — the data landed with no provenance record" if record.is_defective else None),
    )
