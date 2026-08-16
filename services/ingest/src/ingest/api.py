"""The ingest control API — `POST /v1/ingests`, `GET /v1/ingests/{run_id}`.

Two things here are semantics, not decoration, because their absence is what §3.4 calls the estate's
recurring disease:

* **202 means 202.** The handler mints identity, dispatches the workflow, and returns. Enumeration
  and fetching happen after the connection closes. The medallion's head declared 202 and then held
  the request through a sequential per-page harvest.
* **`Idempotency-Key` dedupes the WORK.** A repeat resolves to the same run resource and starts no
  second workflow. The medallion accepted the header and re-harvested the whole volume anyway.

Both are pinned by A1/A2 in `tests/test_ingest_api.py`.

Dedupe keys off the DISPATCH, not off the record's existence. A run whose schedule call never landed
has a record and no executor, and the retry this door advises on a 503 has to be able to drive it —
`RunRecord.scheduled` / `runs.is_redrivable` are that distinction, and without it the advice
guaranteed the zombie it claimed to recover from.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from lance_namespace import PermissionDeniedError
from pydantic import BaseModel, Field

from ingest.auth import AuthSettingsDep, authorize_ingest
from ingest.provenance import ProvenanceRefused
from ingest.runs import (
    SCHEDULE_TIMEOUT_SECONDS,
    RunRecord,
    RunStore,
    ScheduleUnavailable,
    WorkflowRunReader,
    WorkflowStarter,
    is_redrivable,
    merge_workflow_state,
    record_from_workflow_state,
    run_id_for,
)
from ingest.sizing import IngestSizing, SizingRefused, resolve
from ingest.sources import SourceDescriptor, SourceSpec, describe_sources, registered_kinds


logger = logging.getLogger(__name__)


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
    """A source-agnostic request — I1/I2: no source-specific route, no dataset path.

    `project` + `dataset` are this plane's TWO-level addressing and they do NOT mirror the catalog's
    four-level hierarchy (`project > warehouse > namespace > table`). What `catalog_service.ensure`
    actually does: creates a NAMESPACE named after the project, then a table id `{project}${dataset}`.
    So `project` names a namespace, `dataset` is a table, the warehouse is never chosen, and a nested
    namespace is unreachable. Named here so nobody reads these two fields as the hierarchy; the
    conflation itself is open work.
    """

    kind: str = Field(description="registered source kind, e.g. 's3-prefix' | 'local-dir'")
    project: str
    dataset: str
    options: dict[str, object] = Field(default_factory=dict)
    sizing: IngestSizing = Field(default_factory=IngestSizing, description="per-run write partitioning; unset fields use the deployment default")


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


async def _mark_scheduled(store: RunStore, record: RunRecord) -> None:
    """The engine took the run — the one write that makes the dedupe branch dedupe.

    Applied to the record as it stands NOW rather than to the handler's own copy, which went stale
    the moment it awaited the engine. `scheduled` is monotonic — a dispatch that landed cannot
    un-land — so the write itself is unconditional; what it must not do is carry a stale snapshot of
    every OTHER field back over a concurrent attempt's.
    """
    current = await store.get(record.run_id) or record
    await store.put(current.model_copy(update={"scheduled": True}))


async def _release_claim(store: RunStore, record: RunRecord, **update: object) -> None:
    """This attempt ended without scheduling — hand the run back, unless it is no longer ours.

    A compare-and-set on the lease, and it has to be: the handler's copy of the record is stale
    across the `await` on the engine, so writing it back blindly is a lost update whose lost field is
    `scheduled`. The result would be worse than the zombie this whole path exists to remove — a run
    the engine IS executing goes back to re-drivable and stays there, so EVERY later POST on that key
    dispatches it again. `dispatch_started_at` identifies the attempt, so it is also the compare.
    """
    current = await store.get(record.run_id)
    if current is not None and current.dispatch_started_at != record.dispatch_started_at:
        return
    await store.put((current or record).model_copy(update=update))


@router.post("/ingests", status_code=status.HTTP_202_ACCEPTED, response_model=IngestAccepted)
async def create_ingest(
    body: IngestRequest,
    response: Response,
    request: Request,
    store: Annotated[RunStore, Depends(get_store)],
    starter: Annotated[WorkflowStarter, Depends(get_starter)],
    settings: AuthSettingsDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    # The INVOKING Dapr app-id. Without it the door cannot tell a service from the public front
    # door invoking on a stranger's behalf — see `auth.public_callers`.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> IngestAccepted:
    # BEFORE anything else. The body names the project this write lands in, so the admin check targets
    # that project rather than a configured one — authorization scope must equal write scope, or an
    # admin of project A passes the gate while the rows land in project B.
    originator = await authorize_ingest(request, settings, body.project, dapr_api_token, authorization, dapr_caller_app_id)

    if body.kind not in registered_kinds():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown source kind {body.kind!r} — registered: {registered_kinds() or '<none>'}",
        )

    # HERE, at accept, not inside the drain. A `fragment_rows` at or above the queue's
    # `max_ack_pending` does not write bigger fragments — it stops JetStream delivering and the drain
    # waits forever, silently, hours in. Resolving at the door turns that into a 400 naming the
    # ceiling, which is a fix the caller can act on; the resolved numbers then ride the spec so every
    # worker in the fan-out runs the values THIS run was accepted with, rather than re-reading env
    # that may have moved under a rolling restart.
    try:
        sizing = resolve(body.sizing)
    except SizingRefused as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # A token-less call gets a fresh run: without a caller key there is nothing to converge ON, and
    # inventing one would make every retry a new run while pretending otherwise.
    key = idempotency_key or uuid.uuid4().hex
    run_id = run_id_for(body.project, key)

    existing = await store.get(run_id)
    # SAME KEY, DIFFERENT SPEC IS A CONFLICT — on BOTH branches below, which is why it is checked
    # before either. Only `project` and the key go into the run id, so a caller reusing one key for
    # a different `dataset` or `kind` lands on someone else's run. Neither branch noticed: the dedupe
    # branch answered 200 `deduplicated=true` for a run that ingested something ELSE (the caller is
    # told their request was accepted and it never was), and the re-drive branch silently REPURPOSED
    # the record onto the new spec. An Idempotency-Key means "this exact request"; a key reused with
    # a different payload is the one case where the honest answer is neither dedupe nor re-drive.
    if existing is not None and (existing.dataset, existing.kind) != (body.dataset, body.kind):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Idempotency-Key {key!r} already names run {run_id} ingesting "
                f"{existing.kind}:{existing.dataset}, not {body.kind}:{body.dataset} — use a new key for a different request"
            ),
        )
    if existing is not None and not is_redrivable(existing):
        # THE dedupe. Same key + same spec resolves to the same resource and starts NO second
        # workflow — A2 asserts zero new dispatches, not merely a matching id.
        #
        # Gated on `is_redrivable`, and that gate is the whole of the zombie fix. The record is
        # written BEFORE the dispatch so a concurrent GET can see the run, so "a record exists" and
        # "a workflow is executing" are different facts — and reading the first as the second made
        # the 503 below self-defeating: the caller did exactly what its detail said, retried with the
        # same key, and landed here on a run the engine had never been told about.
        response.headers["Location"] = f"/v1/ingests/{run_id}"
        return IngestAccepted(run_id=run_id, status=existing.status, deduplicated=True)

    # Re-drive, or first drive. The existing record is REUSED rather than replaced so `created_at`
    # keeps naming when the caller first asked; `dispatch_started_at` claims the attempt, which is
    # what stops a duplicate arriving mid-dispatch from racing this one to the engine. Status and
    # errors reset because a previous attempt's dispatch failure is not this attempt's outcome.
    #
    # The SPEC comes from THIS request's body, and after the 409 above it is guaranteed to EQUAL the
    # stored one — so the update below is a no-op for `dataset`/`kind` and stays only because the
    # record may be brand new. It used to be load-bearing: the comment here said a re-drive "can
    # legitimately arrive naming a different dataset or kind", which is what let one key silently
    # repurpose another request's run. Dispatching the body while the record named the old table is
    # the one thing a status endpoint must not do; refusing the request outright is better than
    # either half of that.
    record = (existing or RunRecord(run_id=run_id, project=body.project, dataset=body.dataset, kind=body.kind)).model_copy(
        update={
            "project": body.project,
            "dataset": body.dataset,
            "kind": body.kind,
            "status": "ACCEPTED",
            "errors": {},
            "dispatch_started_at": datetime.now(UTC),
        }
    )
    await store.put(record)

    spec = SourceSpec(kind=body.kind, project=body.project, dataset=body.dataset, options=body.options)
    try:
        await starter.start(run_id, {"run_id": run_id, **spec.model_dump(), "sizing": sizing.model_dump(), "originator": originator or ""})
    except (TimeoutError, ScheduleUnavailable) as exc:
        # 503, not 500. A1 bounds the schedule call so a slow sidecar cannot hold the POST past its
        # one-second contract — but the bound turned a BUSY sidecar into an unretryable server error,
        # observed on a pod whose daprd had only just started. 503 + Retry-After is the same
        # information in a form a client can act on, and `@rask/api`'s refuse() already reads the
        # problem+json detail. The run id is deterministic, so the retry converges on this same run
        # rather than starting a second one.
        #
        # BOTH failures, not just the timeout: a sidecar that answers badly is the same retryable
        # condition as one that answers slowly, and mapping only the timeout left every other
        # transport failure as a 500 on top of a stored record — a run with a status and no executor.
        # The adapter draws the line (`_DaprWorkflowStarter`); a programming error is not in it.
        #
        # Releasing the lease is what makes the advice true. The attempt is over, so the record goes
        # back to re-drivable immediately rather than waiting the bound out — a client that ignores
        # Retry-After and comes straight back still gets a dispatch instead of a dedupe. Through
        # `_release_claim` because by now another attempt may own the run (see its docstring).
        await _release_claim(store, record, dispatch_started_at=None)
        timed_out = isinstance(exc, TimeoutError)
        reason = f"did not accept run {run_id} within {SCHEDULE_TIMEOUT_SECONDS}s" if timed_out else f"refused run {run_id}: {str(exc)[:200]}"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"the workflow engine {reason} — the run is NOT scheduled; retry with the same Idempotency-Key and it will be dispatched",
            headers={"Retry-After": "5"},
        ) from exc
    except Exception as exc:
        # NOT retryable, and not swallowed — re-raised, so the 500 and its trace still happen. What
        # changes is the record: leaving an ACCEPTED behind for a dispatch that can never succeed is
        # the same zombie in a different costume, so the run says FAILED and names the reason instead
        # of reporting a status nothing is driving.
        await _release_claim(store, record, status="FAILED", errors={"dispatch": f"{type(exc).__name__}: {exc}"[:500]}, dispatch_started_at=None)
        logger.exception("run %s could not be dispatched and the failure is not retryable", run_id)
        raise

    # Only NOW is the run real. Everything above this line is a claim; the engine accepting the
    # instance is the fact, and it is the fact the dedupe branch reads.
    await _mark_scheduled(store, record)

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


class RunListResponse(BaseModel):
    """Runs this REPLICA accepted, plus the caveat that makes the answer usable.

    `authoritative=False` is the whole point. The store is a process-local read-side index (`get`
    exists so a poll need not query the workflow engine), so a run accepted by another replica is
    absent and a restart empties it. Answering with a bare list would present that as the estate's
    run history, which it is not — the durable list is the LINEAGE graph, and the compute zone's runs
    page reads that for exactly this reason.

    Shipped anyway because the honest local list is genuinely useful — an operator on one pod wants
    to know what that pod took — and because a field that says so costs one line, while a caller
    guessing costs an incident.
    """

    runs: list[RunStatusResponse]
    authoritative: bool = Field(
        default=False,
        description="FALSE always: this is one replica's read-side index, not the estate's run history. Use the lineage graph for that.",
    )


@router.get("/ingests", response_model=RunListResponse)
async def list_ingests(
    request: Request,
    store: Annotated[RunStore, Depends(get_store)],
    settings: AuthSettingsDep,
    limit: int = 50,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> RunListResponse:
    """Recent runs, newest first. Authorized PER RUN, then filtered.

    There is no single project to authorize against before reading — the list spans tenants — so each
    record is checked against its OWN project and the ones the caller may not see are dropped rather
    than refused. A 403 for the whole call would leak that runs exist; an empty list leaks nothing.

    **ONLY a refusal filters.** This used to swallow every exception, and the other two things
    `authorize_ingest` raises are not refusals of a ROW — they are failures of the CALL, and both
    rendered as an empty 200:

      * `ServiceUnavailableError` — OpenFGA or the OIDC verifier is down. Every record then "filters",
        so a fail-closed authorization outage is indistinguishable from a caller who owns nothing.
        That is the worst possible rendering of an outage: it looks like an answer.
      * `UnauthenticatedError` — the caller's own bearer is malformed or invalid. It cannot be true of
        one row and false of the next; answering 200 with `{"runs": []}` tells a caller their token
        works and they have no runs.

    Both propagate now (503 / 401). `PermissionDeniedError` is the one that means "not this row".
    """
    records = await store.recent(min(max(limit, 0), 200))

    visible: list[RunStatusResponse] = []
    for record in records:
        try:
            await authorize_ingest(request, settings, record.project, dapr_api_token, authorization, dapr_caller_app_id)
        except PermissionDeniedError:
            # DEBUG, not warning: a caller seeing only their own runs is the endpoint WORKING.
            # Logging it louder would make a correctly-filtered list look like a stream of
            # authorization failures, which is how a real signal gets tuned out.
            logger.debug("run %s filtered from the list for this caller", record.run_id, exc_info=True)
            continue
        visible.append(RunStatusResponse(**record.model_dump()))

    return RunListResponse(runs=visible)


@router.get("/ingests/{run_id}", response_model=RunStatusResponse)
async def get_ingest(
    run_id: str,
    request: Request,
    store: Annotated[RunStore, Depends(get_store)],
    reader: Annotated[WorkflowRunReader | None, Depends(get_reader)],
    settings: AuthSettingsDep,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    # The INVOKING Dapr app-id. Without it the door cannot tell a service from the public front
    # door invoking on a stranger's behalf — see `auth.public_callers`.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
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

    # Authorized against the run's OWN project, which is only knowable after the record is resolved —
    # a run id names a tenant, and a status body carries that tenant's project, dataset, source keys
    # and error detail. Reading it is not public.
    await authorize_ingest(request, settings, record.project, dapr_api_token, authorization, dapr_caller_app_id)

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
    # A REFUSED read is its own answer, and reporting it is the point. Before this, the credential-less
    # read got a 401, the reader swallowed it as "unreachable", and the endpoint answered
    # `defect: null` — so A8 certified provenance as sound while every lineage event was being
    # rejected. Silence about an unverifiable claim is the same failure the defect field exists to
    # prevent, pointed at the checker instead of the data.
    refused: str | None = None
    if provenance is not None and record.status in ("COMPLETE", "COMPLETE_WITH_ERRORS"):
        try:
            present = await asyncio.to_thread(provenance.has_run, run_id)
        except ProvenanceRefused:
            present = None
            refused = (
                "provenance could NOT be verified: the lineage graph refused this service's read. "
                "The run may or may not have a provenance record — this service has no valid lineage "
                "credential, so its A8 verdict cannot be trusted. Check RASK_LINEAGE_APP_TOKEN and "
                "RASK_LINEAGE_SERVICE_IDENTITY, and that the identity is in LINEAGE_SERVICE_SUBJECTS."
            )
        record = record.model_copy(update={"lineage_run_present": present is not False})

    return RunStatusResponse(
        run_id=record.run_id,
        status=record.status,
        units_total=record.units_total,
        units_done=record.units_done,
        errors=record.errors,
        committed_version=record.committed_version,
        # The refusal OUTRANKS the ordinary defect string: "we could not check" must never be
        # rendered as "we checked and it was fine".
        defect=refused or ("run reports success but no lineage run exists for it — the data landed with no provenance record" if record.is_defective else None),
        published=record.published,
        from_version=record.from_version,
        to_version=record.to_version,
        publish_reason=record.publish_reason,
        publish_error=record.publish_error,
    )
