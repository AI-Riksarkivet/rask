"""The flows HTTP surface: `/flows/catalog`, `/flows/validate`, `/flows/runs[/{run_id}]`.

Mounted under `settings.api_prefix` by `make_service_app`, so the deployed paths are
`/api/flows/...` (the chart sets `RASK_API_PREFIX=/api`, and `scripts/dev-micro.sh` matches it) —
which is exactly what the gateway's `/api/flows` row forwards unrewritten. A router prefix of
`/flows` with an api_prefix of `/api/v1` would serve `/api/v1/flows/...` and every call through the
gateway would 404; that trap has already been paid for once in this estate (the ingest row's `/v1`).

Both `/runs` routes are gated on the estate `writer` tier (`flows.security`), and a run id is
DERIVED from the caller's `Idempotency-Key` rather than minted per attempt — the run id is also the
durable workflow's instance id, so deriving it is what makes a retry converge on the run it is
retrying instead of forking a second one. See `create_run` and `lifespan.DaprFlowScheduler`.
"""

import asyncio
import logging
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from flows import executor, security
from flows.catalog import CATALOG
from flows.dependencies import FlowsSettingsDep, HttpDep, RunReaderDep, RunsDep, SchedulerDep, ScheduleUnconfirmed
from flows.graph import validate_graph
from flows.metrics import record_run
from flows.models import (
    CatalogResponse,
    FlowGraph,
    RunJob,
    RunRefused,
    RunRequest,
    RunState,
    ValidateResponse,
)
from service_kit.exceptions import PROBLEM_JSON, ForbiddenError, NotFoundError, ServiceUnavailableError
from service_kit.governed.audit import ALLOW, DENY, audit


log = logging.getLogger(__name__)

router = APIRouter(prefix="/flows", tags=["flows"])

#: The namespace that makes a run id deterministic across processes, replicas and restarts. A fixed
#: constant, not uuid1/uuid4: the whole point is that the same `Idempotency-Key` yields the same run
#: id — and therefore the same workflow INSTANCE id — on a different pod, after a crash, on a retry.
#: Distinct from `ingest.runs.RUN_NAMESPACE` so the two services cannot collide on one key.
RUN_NAMESPACE = uuid.UUID("2c9b7f41-6d4a-5e18-9a3c-71b0c5d2e480")


def run_id_for(subject: str, idempotency_key: str) -> str:
    """Derive the run id from the CALLER's key — the estate's idempotency pattern (`ingest.runs`).

    Deterministic by construction: same subject + same key -> same id, on any pod, after any crash.
    A token minted per attempt leaves one orphan run per retry, and it is what made a schedule
    timeout unrecoverable: with nothing to converge on, the only choices were "run it again" and
    "lose it".

    Scoped by SUBJECT, because the run id IS the workflow instance id and it is also the URL of a
    readable resource: an unscoped key would let one caller's `Idempotency-Key: 1` land on another's
    run. `ingest` scopes by project for the same reason; a flow has no project to scope to.

    The name is INJECTIVE, and a printable separator is not enough to make it so. `f"{subject}-flows-{key}"`
    lets the delimiter occur inside either field, so distinct pairs collide: subject `alice` with
    `Idempotency-Key: b-flows-c` and subject `alice-flows-b` with key `c` both render
    `alice-flows-b-flows-c` and therefore the same uuid5 — measured, not reasoned:
    both produced `run-ad6bec6da0b75e49a3a396c284f65ae4`. The key half is a caller-supplied HTTP
    header, so one crafted subject is enough to sit on another caller's run: read their node outputs
    at a URL they now share, or have their POST answered as a duplicate of someone else's.

    `\\x00` is the separator because it cannot appear in either field — an HTTP header value cannot
    carry a NUL, and a token `sub` is a JSON string that would have to smuggle one through the
    verifier. That is a property of the alphabet rather than a convention about what people name
    things, which is the only kind of separator argument that survives a hostile input.
    """
    return f"run-{uuid.uuid5(RUN_NAMESPACE, '\x00'.join((subject, 'flows', idempotency_key))).hex}"


@router.get("/catalog", response_model=CatalogResponse)
async def get_catalog(
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
    settings: FlowsSettingsDep,
) -> CatalogResponse:
    """The server-declared node kinds. Shape is pinned — see `models.CatalogResponse`."""
    await security.require_read(checker, settings, subject)
    return CatalogResponse(kinds=CATALOG)


@router.post("/validate", response_model=ValidateResponse)
async def validate(
    graph: FlowGraph,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
    settings: FlowsSettingsDep,
) -> ValidateResponse:
    """Graph hygiene, with no cluster involved: duplicate ids, unknown kinds, dangling edges,
    self-loops, cycles. Same vocabulary the frontend executor refuses on.

    GATED since 2026-08-26 (owner ruling: the estate is authenticated). The graph is also bounded at
    the MODEL now — `FlowGraph.nodes` carries `max_length` — because the ceiling in `validate_graph`
    fires only after every node has been built, which bounded the answer and not the work.
    """
    await security.require_read(checker, settings, subject)
    problems = validate_graph(graph)
    return ValidateResponse(ok=not problems, problems=problems)


@router.post(
    "/runs",
    response_model=RunState,
    # HTTPStatus, not `fastapi.status.HTTP_422_UNPROCESSABLE_ENTITY`: starlette deprecated that alias
    # in favour of ..._CONTENT and emits a warning at IMPORT of this module. `service_kit.exceptions`
    # already builds its problem bodies from `http.HTTPStatus`, so this is the estate's spelling too.
    responses={HTTPStatus.UNPROCESSABLE_ENTITY: {"model": RunRefused, "description": "The graph does not validate."}},
)
async def create_run(
    request: RunRequest,
    http: HttpDep,
    settings: FlowsSettingsDep,
    runs: RunsDep,
    scheduler: SchedulerDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RunState | JSONResponse:
    """Execute a flow.

    Validation first, always: an invalid graph is refused with the problem LIST rather than
    half-executed. Then one of two lanes, decided at startup and not by the caller — the durable
    Dapr workflow if a sidecar was found, otherwise inline.

    `Idempotency-Key` makes a RETRY safe: the run id is DERIVED from it (`run_id_for`), so a request
    made after the first one answered resolves to the same resource and the same workflow instance
    rather than to a second run. Two keyed POSTs in flight *simultaneously* converge only in the
    durable lane, where the engine refuses the duplicate instance id — see the dedupe below for the
    exact boundary. A key-less POST still gets a fresh run: without a caller key there is nothing to
    converge on, and inventing one would make every retry a new run while pretending otherwise
    (`ingest.api`).

    The refusal is a `JSONResponse` rather than a raised `UnprocessableEntityError` for one reason:
    `service_kit.exceptions._problem` flattens a problem to a single `detail` string, and the
    builder needs the list to highlight nodes. The media type and the other four members match the
    estate's problem+json exactly, so a client parses this like any other refusal.
    """
    problems = validate_graph(request.graph)
    if problems:
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            media_type=PROBLEM_JSON,
            content=RunRefused(detail=f"{len(problems)} problem(s) in the graph", problems=problems).model_dump(),
        )

    # AUTHZ, after shape (identity 401 → shape 422 → authz 403, the estate's door order). A flow's
    # model nodes invoke live Serve endpoints, so this is the one route that spends compute. The
    # denial NAMES the missing tuple — the estate's FGA-denial format, so the fix is in the message.
    obj = settings.fga_root_object
    allowed = await checker(user=subject, relation=security.EXECUTE, obj=obj)
    audit("flows_run", ALLOW if allowed else DENY, subject=subject, resource=obj)
    if not allowed:
        raise ForbiddenError(f"'{subject}' lacks '{security.EXECUTE}' on '{obj}' — running a flow needs the estate writer tier")

    # A key-less call gets a fresh run: `run_id_for` still derives it, so one code path mints every
    # run id, and the randomness lives in the KEY rather than in a second id scheme.
    run_id = run_id_for(subject, idempotency_key or uuid.uuid4().hex)

    existing = runs.get(run_id)
    if existing is not None:
        # The dedupe a RETRY sees: once a run has been recorded, the same key + subject resolves to
        # that resource and starts nothing — the property that makes the 503 below retryable rather
        # than a coin flip. It keys off the RECORD, and `_remember` writes that record only after the
        # lane finishes, so what it covers is a retry made after the first attempt answered. Two
        # keyed POSTs in flight at once both fall through here, and what separates them is the lane,
        # not this line: the durable one dedupes at the DISPATCH (the derived id is the workflow
        # instance id, so the engine refuses the second create as a duplicate and
        # `DaprFlowScheduler` reports the run as durable), while the inline one has no dispatch to
        # dedupe on and runs the graph twice. That is the honest cost of a process-local v0 store
        # (`dependencies.get_runs`) and it is paid only where there is no sidecar; `ingest.api`'s
        # header states the general rule — dedupe keys off the dispatch, not off a record's
        # existence.
        return existing

    state: RunState | None = None
    if scheduler is not None:
        job = RunJob(
            graph=request.graph,
            seeds=request.seeds,
            serve_url=settings.serve_url,
            serve_timeout=settings.serve_timeout,
        )
        try:
            await scheduler.schedule(run_id, job.model_dump())
        except ScheduleUnconfirmed as exc:
            # NOT the degrade path. The scheduler could not establish whether the instance exists, and
            # `asyncio.wait_for` cancels the wait rather than the gRPC call — so degrading here is how
            # one run executes twice, once durably and once inline, with the caller told about the
            # inline one. 503 is the honest answer and the retryable one: the run id is derived from
            # the caller's key, so retrying with that same key converges on THIS run.
            log.warning("schedule for %s was not confirmed — refusing rather than running it twice", run_id)
            # The scheduler's own sentence carries the bound it actually waited, so the number in the
            # message cannot drift from the number in the code.
            raise ServiceUnavailableError(f"{exc} — retry with the same Idempotency-Key") from exc
        except Exception:
            # DEGRADE to the inline lane rather than 500 — reached only when the scheduler has
            # established that no instance was created (it probes the engine before raising this).
            # A sandbox run must not fail because the DURABLE lane is unavailable: the graph is
            # perfectly runnable here and now, and a 500 tells the user nothing they can act on.
            # Measured live 2026-08-06: a sidecar was injected (so the runtime started and this branch
            # was taken) while `lance-statestore` was not SCOPED to app-id `flows`, so
            # `create_workflow_instance` raised "the state store is not configured to use the actor
            # runtime" — a component the estate does declare with `actorStateStore: "true"`, just not
            # visibly to this app. Logged at exception level because a silently-inline run is the
            # thing an operator must be able to find; the caller still gets a completed run.
            log.exception("durable lane unavailable for %s — falling back to the inline executor", run_id)
        else:
            # `running`, with no node states yet: the engine owns the run from here. Reading the live
            # per-node state back out of the workflow history is the follow-up (open_studio_flows.md
            # defers streaming per-node progress) — v0 proves the seam, it does not stream it.
            state = RunState(run_id=run_id, status="running")
            record_run("durable")

    if state is None:
        # The lane is decided here, per request — the startup log announces only what the process COULD
        # do, not what this run did.
        record_run("inline")
        state = await executor.execute(
            request.graph,
            request.seeds,
            run_id,
            client=http,
            serve_url=settings.serve_url,
            serve_timeout=settings.serve_timeout,
        )

    _remember(runs, state, settings.max_runs)
    return state


class TerminateAccepted(BaseModel):
    """202 for a request the ENGINE accepted, not a completed stop.

    The SDK's real semantics, stated in the body rather than implied by the status: further
    scheduling stops, but an activity already in flight runs to completion. A caller told "terminated"
    would reasonably assume the Serve replicas were free, and they are not — the same wording
    `ingest.api.TerminateAccepted` uses, for the same reason.
    """

    run_id: str
    detail: str = "further scheduling stops; work already in flight may still complete"


@router.post("/runs/{run_id}/terminate", response_model=TerminateAccepted, status_code=HTTPStatus.ACCEPTED)
async def terminate_run(
    run_id: str,
    reader: RunReaderDep,
    settings: FlowsSettingsDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
) -> TerminateAccepted:
    """Stop a durable run (DWF-MGT-003).

    Without this a run could be started and read but never stopped. `flow_run_workflow` fans out
    `run_node` activities against LIVE Ray Serve endpoints, and a wide graph aimed at a wedged Serve
    deployment occupies replicas for `serve_timeout` x NODE_RETRY per node per wave with no way to
    intervene — a pod restart does not help, because the instance is durable and resumes.

    THE SAME DOOR as start and read, and the same relation: whoever may spend the estate's GPU on a
    drawn graph is who may stop spending it. A narrower gate here would mean the person who started a
    runaway could not stop it.

    AUTHZ BEFORE EXISTENCE, matching `get_run`: no run-id oracle for an unpermitted caller.
    """
    obj = settings.fga_root_object
    allowed = await checker(user=subject, relation=security.EXECUTE, obj=obj)
    audit("flows_run_terminate", ALLOW if allowed else DENY, subject=subject, resource=obj)
    if not allowed:
        raise ForbiddenError(f"'{subject}' lacks '{security.EXECUTE}' on '{obj}' — stopping a run needs the estate writer tier")
    if reader is None:
        raise ServiceUnavailableError("the workflow engine is not reachable from this pod")
    # Through a thread: `DaprWorkflowClient` is synchronous gRPC, and blocking the loop here would
    # stall every other request on the worker — the same rule `get_run` follows for `state`.
    await asyncio.to_thread(reader.terminate, run_id)
    return TerminateAccepted(run_id=run_id)


@router.get("/runs/{run_id}", response_model=RunState)
async def get_run(
    run_id: str,
    runs: RunsDep,
    reader: RunReaderDep,
    settings: FlowsSettingsDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
) -> RunState:
    """Read a run back.

    The SAME door as `POST /runs`, and the same relation. A run document carries what the model
    nodes returned — transcribed page text, prompt output, upstream error strings — so leaving the
    read ungated made the write gate cosmetic: anyone could read the product of the compute they
    were refused permission to spend. The tier is not too strict for its own resource either, since
    nobody can create a run without already holding it.

    AUTHZ BEFORE EXISTENCE: a 404 that only a permitted caller can distinguish from a 403 is one
    less oracle for guessing run ids.
    """
    obj = settings.fga_root_object
    allowed = await checker(user=subject, relation=security.EXECUTE, obj=obj)
    audit("flows_run_read", ALLOW if allowed else DENY, subject=subject, resource=obj)
    if not allowed:
        raise ForbiddenError(f"'{subject}' lacks '{security.EXECUTE}' on '{obj}' — reading a run needs the estate writer tier")

    # The ENGINE first, the local dict second. In the durable lane the dict only ever holds
    # `RunState(status="running")` with an empty `nodes` map — written once by `_remember` and never
    # updated — so preferring it would report a completed run as running forever, hide a FAILED run's
    # error, and 404 every run older than `max_runs` or predating a restart, all while the workflow
    # itself was fine. The workflow RETURNS `RunState.model_dump()`, so the engine holds the real
    # answer and the dict is the cache its own docstring claims to be.
    engine = await asyncio.to_thread(reader.state, run_id) if reader is not None else None
    live = _state_from_engine(run_id, engine)
    if live is not None:
        return live

    state = runs.get(run_id)
    if state is None:
        raise NotFoundError(f"no such run: {run_id}")
    return state


def _state_from_engine(run_id: str, engine: dict[str, object] | None) -> RunState | None:
    """Rebuild a `RunState` from the workflow instance, or None when the engine cannot answer.

    Returning None rather than a placeholder is the point: "the engine has nothing" and "the engine
    says it is running" are different facts, and only the first may fall back to the local record.
    """
    if not engine:
        return None
    status = str(engine.get("runtime_status") or "")

    # COMPLETED means the workflow RETURNED — and what it returned is a whole RunState, node states
    # included. Parse it rather than synthesising one: the workflow already decided succeeded-vs-failed
    # by inspecting its own node results, and re-deriving that here would be a second opinion that can
    # disagree with the run's own record of itself.
    if status == "COMPLETED":
        output = engine.get("serialized_output")
        if isinstance(output, str) and output:
            try:
                return RunState.model_validate_json(output)
            except ValidationError:
                # A completed instance whose output will not parse is a contract break between this
                # reader and the workflow, not a missing run. Say so, rather than reporting the run as
                # absent and sending an operator to look for a run that plainly exists.
                log.exception("run %s completed but its output does not parse as a RunState", run_id)
                return RunState(run_id=run_id, status="failed", error="the run completed but its result could not be read")
        return RunState(run_id=run_id, status="failed", error="the run completed but returned no result")

    # FAILED/TERMINATED never produced a RunState — the body raised or was killed — so this is the one
    # place the reader must compose the answer itself.
    if status in ("FAILED", "TERMINATED"):
        detail = engine.get("failure_details") or engine.get("_failure_detail")
        return RunState(run_id=run_id, status="failed", error=str(detail) if detail else f"the workflow engine reports the run as {status}")

    # RUNNING / PENDING / SUSPENDED — genuinely still going. Per-node progress is not streamed yet
    # (open_studio_flows.md defers it), so the honest answer is the status without invented node
    # states; the local record, if any, carries no more than this either.
    if status:
        return RunState(run_id=run_id, status="running")
    return None


def _remember(runs: dict[str, RunState], state: RunState, max_runs: int) -> None:
    """Store the run, evicting the oldest first. A dict preserves insertion order, so "oldest" is
    the first key — no timestamps, and therefore nothing that can disagree with the run's own."""
    runs[state.run_id] = state
    while len(runs) > max_runs:
        runs.pop(next(iter(runs)))
