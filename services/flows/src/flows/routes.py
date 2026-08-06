"""The flows HTTP surface: `/flows/catalog`, `/flows/validate`, `/flows/runs[/{run_id}]`.

Mounted under `settings.api_prefix` by `make_service_app`, so the deployed paths are
`/api/flows/...` (the chart sets `RASK_API_PREFIX=/api`, and `scripts/dev-micro.sh` matches it) —
which is exactly what the gateway's `/api/flows` row forwards unrewritten. A router prefix of
`/flows` with an api_prefix of `/api/v1` would serve `/api/v1/flows/...` and every call through the
gateway would 404; that trap has already been paid for once in this estate (the ingest row's `/v1`).
"""

import logging
import uuid
from http import HTTPStatus

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from flows import executor
from flows.catalog import CATALOG
from flows.dependencies import FlowsSettingsDep, HttpDep, RunsDep, SchedulerDep
from flows.graph import validate_graph
from flows.models import (
    CatalogResponse,
    FlowGraph,
    RunJob,
    RunRefused,
    RunRequest,
    RunState,
    ValidateResponse,
)
from service_kit.exceptions import PROBLEM_JSON, NotFoundError


log = logging.getLogger(__name__)

router = APIRouter(prefix="/flows", tags=["flows"])


@router.get("/catalog", response_model=CatalogResponse)
async def get_catalog() -> CatalogResponse:
    """The server-declared node kinds. Shape is pinned — see `models.CatalogResponse`."""
    return CatalogResponse(kinds=CATALOG)


@router.post("/validate", response_model=ValidateResponse)
async def validate(graph: FlowGraph) -> ValidateResponse:
    """Graph hygiene, with no cluster involved: duplicate ids, unknown kinds, dangling edges,
    self-loops, cycles. Same vocabulary the frontend executor refuses on."""
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
) -> RunState | JSONResponse:
    """Execute a flow.

    Validation first, always: an invalid graph is refused with the problem LIST rather than
    half-executed. Then one of two lanes, decided at startup and not by the caller — the durable
    Dapr workflow if a sidecar was found, otherwise inline.

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

    run_id = f"run-{uuid.uuid4().hex[:12]}"

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
        except Exception:
            # DEGRADE to the inline lane rather than 500. A sandbox run must not fail because the
            # DURABLE lane is unavailable — the graph is perfectly runnable here and now, and a 500
            # tells the user nothing they can act on. Measured live 2026-08-06: a sidecar was injected
            # (so the runtime started and this branch was taken) while `lance-statestore` was not
            # SCOPED to app-id `flows`, so `create_workflow_instance` raised
            # "the state store is not configured to use the actor runtime" — a component the estate
            # does declare with `actorStateStore: "true"`, just not visibly to this app. Logged at
            # exception level because a silently-inline run is the thing an operator must be able to
            # find; the caller still gets a completed run.
            log.exception("durable lane unavailable for %s — falling back to the inline executor", run_id)
        else:
            # `running`, with no node states yet: the engine owns the run from here. Reading the live
            # per-node state back out of the workflow history is the follow-up (open_studio_flows.md
            # defers streaming per-node progress) — v0 proves the seam, it does not stream it.
            state = RunState(run_id=run_id, status="running")

    if state is None:
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


@router.get("/runs/{run_id}", response_model=RunState)
async def get_run(run_id: str, runs: RunsDep) -> RunState:
    state = runs.get(run_id)
    if state is None:
        raise NotFoundError(f"no such run: {run_id}")
    return state


def _remember(runs: dict[str, RunState], state: RunState, max_runs: int) -> None:
    """Store the run, evicting the oldest first. A dict preserves insertion order, so "oldest" is
    the first key — no timestamps, and therefore nothing that can disagree with the run's own."""
    runs[state.run_id] = state
    while len(runs) > max_runs:
        runs.pop(next(iter(runs)))
