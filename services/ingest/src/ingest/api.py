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

from ingest.runs import (
    SCHEDULE_TIMEOUT_SECONDS,
    RunRecord,
    RunStore,
    WorkflowRunReader,
    WorkflowStarter,
    merge_workflow_state,
    record_from_workflow_state,
    run_id_for,
)
from ingest.sources import SourceDescriptor, SourceSpec, describe_sources, registered_kinds


router = APIRouter(tags=["ingest"])


@router.get("/sources", response_model=list[SourceDescriptor])
async def list_sources() -> list[SourceDescriptor]:
    """The registered source kinds and the options each takes.

    Exists so a caller can OFFER the kinds that are actually registered instead of a list someone
    typed. Without it the registry is readable only from inside the process, and every client
    restates the adapters — which is how the compute zone's form came to hardcode `kind: 'iiif'`
    directly under a comment explaining that the door is source-agnostic, and how `S3PrefixSource`
    stayed unreachable for months while being written and tested.

    A registry nothing can read drifts by construction.
    """
    return describe_sources()


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
    try:
        await starter.start(run_id, {"run_id": run_id, **spec.model_dump()})
    except TimeoutError as exc:
        # 503, not 500. A1 bounds the schedule call so a slow sidecar cannot hold the POST past its
        # one-second contract — but the bound turned a BUSY sidecar into an unretryable server error,
        # observed on a pod whose daprd had only just started. 503 + Retry-After is the same
        # information in a form a client can act on, and `@rask/api`'s refuse() already reads the
        # problem+json detail. The run id is deterministic, so the retry converges on this same run
        # rather than starting a second one.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"the workflow engine did not accept run {run_id} within {SCHEDULE_TIMEOUT_SECONDS}s — retry with the same Idempotency-Key",
            headers={"Retry-After": "5"},
        ) from exc

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
    #: § D2. A COMMIT IS NOT A PUBLICATION — these say whether the committed version is consumable.
    #: `published=false` with a `publish_reason` is the quality gate refusing the DATA (the run itself
    #: was fine); with a `publish_error` it is the publish call not completing. Both leave the
    #: previously published version serving, and both must be visible: a run that committed and did
    #: not publish looks identical to a published one otherwise.
    published: bool | None = None
    from_version: int | None = None
    to_version: int | None = None
    publish_reason: str | None = None
    publish_error: str | None = None


@router.get("/ingests/{run_id}", response_model=RunStatusResponse)
async def get_ingest(
    run_id: str,
    request: Request,
    store: Annotated[RunStore, Depends(get_store)],
    reader: Annotated[WorkflowRunReader | None, Depends(get_reader)],
) -> RunStatusResponse:
    record = await store.get(run_id)
    engine_state = await asyncio.to_thread(reader.state, run_id) if reader is not None else None

    if record is None:
        # A cache miss is not a missing run. The store is process-local, so a pod restart loses every
        # accepted record while the workflows themselves keep executing durably — A3 killed a pod
        # mid-run and the API answered 404 for a run that was still working. The engine holds the
        # POST's own payload as the workflow input, so the record is rebuildable from it.
        record = record_from_workflow_state(run_id, engine_state)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no ingest run {run_id!r}")
        await store.put(record)

    # The store holds only what the caller asked for; everything that MOVES is read from the engine.
    # Without this a completed run reported ACCEPTED forever — nothing writes the record a second
    # time, and nothing should, because the workflow's durable history already is the run's state.
    # `to_thread` because the Dapr client is synchronous gRPC: called inline it blocks the event loop
    # for every other request on the pod, which is the same defect the POST path had to fix.
    record = merge_workflow_state(record, engine_state)

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
        published=record.published,
        from_version=record.from_version,
        to_version=record.to_version,
        publish_reason=record.publish_reason,
        publish_error=record.publish_error,
    )
