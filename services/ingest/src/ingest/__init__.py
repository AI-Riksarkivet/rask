"""services/ingest — pre-bronze acquisition as a platform plane.

Composed through `service_kit.make_service_app` like every other fleet member, so config, error
handlers, middleware, OTel and the Dapr client come from one place (rask-architecture's
entrypoint-over-package contract) rather than being re-assembled here.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

from ingest.api import router as ingest_router
from ingest.health import router as health_router
from ingest.provenance import LineageProvenanceReader
from ingest.queue_health import router as queue_health_router
from ingest.runs import SCHEDULE_TIMEOUT_SECONDS, InMemoryRunStore, ScheduleUnavailable
from service_kit.governed.actor_state_store import probe_actor_state_store
from service_kit.lakehouse.ns_errors import install_problem_handlers


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastapi import FastAPI


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build the ingest app.

    The run store and the workflow starter hang off `app.state` rather than being imported at module
    scope, so a test can substitute either without a live daprd — which is what keeps A1/A2 fast unit
    tests instead of requiring a cluster to assert a contract that is pure request handling.
    """
    # Populates the SourceAdapter registry by import (I1). A deliberate import-time side effect — the
    # alternative is a hand-maintained list elsewhere, which is exactly the drift the registry exists
    # to prevent.
    from ingest.adapters import register_builtin_sources
    from service_kit import make_service_app

    register_builtin_sources()

    app = make_service_app(title="ingest", routers=[health_router, queue_health_router, ingest_router], lifespan=_lifespan)
    app.state.run_store = InMemoryRunStore()
    app.state.workflow_starter = _DaprWorkflowStarter()
    app.state.workflow_reader = _DaprWorkflowReader()
    app.state.workflow_terminator = _DaprWorkflowTerminator()
    app.state.provenance_reader = LineageProvenanceReader()
    # The incremental poll (TRIGGER-2), mounted ONLY when a Dapr cron binding names it. Unmounted by
    # default because a cron route with no cron behind it is a door into starting ingest runs that
    # exists for no reason. Root-mounted by `mount_incremental_cron`: the sidecar delivers an input
    # binding to POST /<component name> at the pod root, so the component name, the env var and the
    # served path are one string.
    from ingest.cron import mount_incremental_cron

    if mount_incremental_cron(app, os.environ.get("RASK_INGEST_CRON_BINDING_NAME")):
        logger.info("ingest incremental cron mounted at /%s", os.environ["RASK_INGEST_CRON_BINDING_NAME"])
    # The authz/authn clients the ingest door needs. Wired here rather than lazily inside the
    # dependency because `authorize_ingest` FAILS CLOSED on a missing client — a lazily-absent client
    # would 503 every request instead of authorizing it, and the estate's review found exactly that
    # bypass shape once already (a gate silently off with FGA_ENABLED=true because the client was
    # never built).
    _wire_auth(app)
    # A DENIAL MUST BE A 403, NOT A 500. `make_service_app` carries the fleet's own handlers, which do
    # not know the `lance_namespace` typed errors the auth door raises — so without this an
    # unauthorized ingest answered "Internal Server Error", which tells the caller nothing, tells
    # monitoring the wrong thing, and would have had someone debugging a crash that was the gate
    # working. The same handler every governed service installs; the estate's rule is that endpoints
    # raise typed errors and ONE translator maps all 22 codes.
    install_problem_handlers(app, logger)
    return app


def _wire_auth(app: FastAPI) -> None:
    """Build the FGA client and OIDC verifier onto `app.state`, or leave them None when disabled.

    Same construction shape as the catalog, lineage and medallion consumers — pinned store/model else
    provision, explicit timeout — so all of them behave alike. A per-service variation here is how one
    pod ends up minting a model version on every restart.
    """
    from ingest.auth import get_auth_settings

    settings = get_auth_settings()

    app.state.fga = None
    if settings.fga_enabled:
        from service_kit.governed import fga

        store_id, model_id = settings.fga_store_id, settings.fga_model_id
        if store_id and model_id:
            app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
        # UNPINNED is not a failure here — it is resolved in the lifespan, where there is an event
        # loop (`_resolve_fga_client`). Building nothing and leaving `app.state.fga` None keeps the
        # fail-closed property intact for the window before startup completes.

    app.state.oidc = None
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_audience:
        from service_kit.governed.oidc import OIDCVerifier

        app.state.oidc = OIDCVerifier(
            settings.oidc_issuer,
            settings.oidc_audience,
            settings.oidc_cache_ttl,
            leeway=settings.oidc_leeway,
            allow_insecure=settings.oidc_allow_insecure,
            # SPLIT-HORIZON DISCOVERY, and the ONLY door in the estate that was missing it. The issuer
            # is the browser-facing URL that lands in a token's `iss` claim (`http://localhost:8080/dex`
            # in k3s); the discovery document has to be fetched from the in-cluster service
            # (`http://rask-dex:5556/dex`). Without the override the verifier fetches discovery from the
            # issuer, which resolves to the POD ITSELF, and every user-bearer ingest died with
            #
            #     httpx.ConnectError: [Errno 111] Connection refused
            #
            # surfaced to the browser as `{"message":"Internal Error"}` — a 500 with nothing about auth
            # in it. The service-token path never touched the verifier, so every in-cluster test passed
            # and only a real signed-in submit from `/compute/etl` reached the line.
            #
            # The chart has been setting `LANCE_OIDC_DISCOVERY_URL` all along and `GovernedAuthSettings`
            # has been parsing it; this door simply never passed it on, while catalog, lineage, viewer,
            # annotator and medallion all did (identical expression, five sites).
            discovery_overrides=({settings.oidc_issuer: settings.oidc_discovery_url} if settings.oidc_discovery_url else None),
        )


async def _resolve_fga_client(app: FastAPI) -> None:
    """When the store/model are UNPINNED, resolve the one the estate already uses. Read-only.

    THIS PLANE STILL REFUSES TO PROVISION. A data writer that mints a store or writes an authorization
    model becomes the source of truth for everyone else's permissions, and that is not ingest's job.
    But it applied that principle to the LOOKUP as well, and reading which store exists is not
    authoring one — so ingest ended up the only service in the estate that cannot boot on the chart's
    own default posture.

    `auth.fgaStoreId: ""` is that posture, and it is correct: a store id is a per-cluster ULID, so it
    cannot be a committed chart default. Catalog, lineage and medallion resolve by name and come up;
    ingest 503'd every user-bearer request on every dev and e2e cluster. It reaches a person as
    `{"message":"Internal Error"}` from an ETL submit, and the only trace is a startup warning.

    The stopgap was `kubectl set env LANCE_FGA_STORE_ID=…` on the live deployment, which drifts from
    the chart and dies at the next `make k3s-up` — a fix that has to be reapplied by hand is a defect
    wearing a workaround.

    HERE rather than in `_wire_auth` because resolving is I/O and `_wire_auth` is sync. Leaving
    `app.state.fga` None until startup completes keeps the fail-closed window closed: `authorize_ingest`
    503s on a missing client, which is the correct answer before the estate is known to be reachable.

    Still fails closed after: `fga.resolve` returns None when no store or model exists, meaning the
    estate has not been bootstrapped, and the client stays unwired.
    """
    from ingest.auth import get_auth_settings

    settings = get_auth_settings()
    if not settings.fga_enabled or app.state.fga is not None:
        return
    from service_kit.governed import fga

    try:
        resolved = await fga.resolve(settings.fga_api_url)
    except Exception:
        # Never fatal at startup. OpenFGA being slow to accept connections is an ordering blip, not a
        # reason to CrashLoopBackOff; the door 503s until it is reachable, which is where an operator
        # can actually see it.
        logger.warning("ingest: could not reach OpenFGA to resolve a store — the ingest door will 503", exc_info=True)
        return
    if resolved is None:
        logger.warning("ingest: LANCE_FGA_ENABLED but no provisioned OpenFGA store to resolve — the ingest door will 503")
        return
    store_id, model_id = resolved
    app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
    logger.info("ingest: resolved the OpenFGA store by name (unpinned) — pin LANCE_FGA_STORE_ID/MODEL_ID for production")


def _lifespan(settings: Any) -> Any:  # noqa: ANN401 — service_kit's LifespanFactory shape
    """Start the Dapr WorkflowRuntime for the lifetime of the process.

    THE WORKER. Without this the service can schedule a workflow and nothing will ever execute it:
    `DaprWorkflowClient` only enqueues, and the runtime is what registers the definitions and pulls
    work. The first in-cluster deploy had the engine running in the sidecar
    (`Workflow engine started`) and still could not run a workflow, because the APP side was absent —
    an asymmetry that looks healthy from every angle except an actual run.

    Registration lives in `ingest.workflow.register()` so there is ONE list of workflows and
    activities. A definition registered in the API process but not the worker fails at runtime with
    an unhelpful "no such activity", which is the failure mode that argues for a single registry.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await _resolve_fga_client(app)
        runtime = None
        try:
            import dapr.ext.workflow as wf

            from ingest.workflow import register

            runtime = wf.WorkflowRuntime()
            register(runtime)
            # start() spawns the worker's own threads; it does not block the event loop.
            runtime.start()
            app.state.workflow_runtime = runtime
            logger.info("dapr workflow runtime started")
            # The line above is TRUE and INSUFFICIENT: the runtime starts whether or not this
            # app-id can reach an actor state store, and without one the first call fails (and,
            # on dapr 1.18.1, panics the sidecar). Ask the sidecar what it can actually see.
            await probe_actor_state_store(capability="no ingest run can start")
        except Exception:
            # Deliberately non-fatal. The health probe answers process liveness (see health.py), and
            # a service that refuses to start because its sidecar is not up yet turns an ordering
            # blip into a CrashLoopBackOff. Runs will fail loudly at schedule time instead, which is
            # where the operator can actually see them.
            logger.warning("dapr workflow runtime unavailable — runs cannot execute", exc_info=True)
            app.state.workflow_runtime = None
        try:
            yield
        finally:
            if runtime is not None:
                with_suppressed = getattr(runtime, "shutdown", None)
                if callable(with_suppressed):
                    with_suppressed()

    return lifespan


def _is_already_scheduled(exc: BaseException) -> bool:
    """True when the engine refused because it ALREADY holds this instance — a success, not a failure.

    `instance_id` is `run_id_for(project, key)`, which is deterministic, so the instance the engine is
    complaining about IS this run. The condition is reachable by design: `asyncio.wait_for` cancels
    the await, never the thread behind `to_thread`, so a schedule that was merely slow still lands and
    the retry the 503 advises then meets its own earlier dispatch. Reporting that as an error would
    make the advice impossible to satisfy; dispatching past it would run the harvest twice.

    Matched on the message because neither dapr-ext-workflow nor durabletask exports a typed
    already-exists error and the gRPC status behind it has moved between versions. Narrow enough to be
    safe: a false positive needs a schedule failure whose own text says the instance exists.
    """
    return "already exists" in str(exc).lower()


#: The transport errors, named rather than imported. `(module, attribute)` pairs resolved at call
#: time so this module keeps no import-time dependency on grpc or dapr — the same reason
#: `_DaprWorkflowStarter` imports lazily — and no STATIC dependency on two libraries that ship no
#: stubs into a plane whose type gate treats a warning as a failure.
_SIDECAR_ERROR_NAMES = (("grpc", "RpcError"), ("dapr.clients.exceptions", "DaprInternalError"))


def _sidecar_error_types() -> tuple[type[BaseException], ...]:
    """The exception types that mean the SIDECAR answered badly.

    Type-based rather than status-code-based on purpose. Guessing which gRPC codes are transient is
    how a permanent misconfiguration turns into an infinite client retry loop, and the code dapr
    returns for one condition has moved across versions. What is stable is the boundary: an error
    raised by the transport is an operator's problem and a caller may retry it; anything else raised
    in here — a payload that will not serialize, a name that does not resolve — is this service's own
    bug and keeps its 500.

    `OSError` covers the socket dying under the channel; there is no file I/O on this path for it to
    over-match.
    """
    types: list[type[BaseException]] = [OSError]
    for module_name, attribute in _SIDECAR_ERROR_NAMES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover — both ship with dapr; absent only in a stripped env
            continue
        candidate = getattr(module, attribute, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            types.append(candidate)
    return tuple(types)


class _DaprWorkflowStarter:
    """Schedules `ingest_run` through the Dapr workflow client in the sidecar.

    Imported lazily inside `start` so constructing the app — which every test does — never requires a
    reachable sidecar.

    Also the place the door's failure vocabulary is DEFINED. `api.py` knows nothing about gRPC and
    should not: it maps `TimeoutError` and `ScheduleUnavailable` to a retryable 503 and lets anything
    else be a 500, so the classification has to happen at the one seam that can see a dapr exception.
    """

    async def start(self, run_id: str, payload: dict[str, object]) -> None:
        import dapr.ext.workflow as wf

        from ingest.workflow import ingest_run

        def _schedule() -> None:
            client = wf.DaprWorkflowClient()
            client.schedule_new_workflow(workflow=ingest_run, input=payload, instance_id=run_id)

        # to_thread + a timeout: the client is SYNCHRONOUS gRPC, so calling it directly would block
        # the event loop for every other request, and calling it unbounded would hold the POST open
        # past A1's one-second contract. A scheduling failure surfaces as an error on the run rather
        # than a hung connection — a caller that gets no answer cannot retry intelligently.
        try:
            await asyncio.wait_for(asyncio.to_thread(_schedule), timeout=SCHEDULE_TIMEOUT_SECONDS)
        except TimeoutError:
            # Straight through — the bound is this method's own contract and `api.py` already maps it.
            # Letting it reach the classifier below would put a decided outcome at the mercy of
            # whatever text the exception happens to carry.
            raise
        except Exception as exc:
            if _is_already_scheduled(exc):
                logger.info("run %s is already scheduled — the engine holds the instance; converging on it", run_id)
                return
            if isinstance(exc, _sidecar_error_types()):
                raise ScheduleUnavailable(str(exc) or type(exc).__name__) from exc
            raise


class _DaprWorkflowTerminator:
    """Stops a live run, for `POST /v1/ingests/{id}/terminate` (DWF-MGT-003).

    Before this seam existed the service exposed start and status and no lifecycle control at all, so
    a run that was WRONG rather than broken — pointed at the wrong prefix, or enumerating a bucket
    somebody meant to narrow — could not be stopped. It held its JetStream subject and its per-run
    durable and kept committing. Neither `max_units` (which refuses at enumeration, before the
    fan-out) nor `max_run_hours` (a deadline, whose in-code default is 0 = unbounded) is a brake.

    BOUNDED, NOT INSTANT, and the route says so. Terminate is recursive by default so it reaches the
    chunk children, but it stops further SCHEDULING — an in-flight activity such as a `drain_chunk`
    mid-fetch runs to completion. That is the same SDK limit `terminate_chunks` documents.

    A class rather than a bare call so it can be replaced in tests, matching the starter and reader
    beside it: the alternative is a route that cannot be exercised without a sidecar.
    """

    def terminate(self, run_id: str, reason: str = "") -> bool:
        """Ask the run to end. True when the engine accepted the request.

        RAISES AN EVENT rather than terminating the instance, and that is the whole fix.
        `terminate_workflow` sets the instance TERMINATED and never resumes the generator, so
        `emit_terminal` — the ONLY caller of `release_run_units` — never runs. The run's JetStream
        subject and its per-run durable consumer were left behind permanently (WORK_QUEUE retention
        means a message leaves only when acked, and no consumer for that run id is created again),
        and no FAIL record reached lineage: the run simply vanished. That is the
        `messages: 1, consumers: 0` state `emit_terminal`'s release comment records from the live
        estate.

        The cost, accepted knowingly (owner ruling, 2026-08-25): this is asynchronous. It asks the
        run to stop at its next select rather than stopping it, so a parent wedged before that point
        will not honour it. What it buys is ONE cleanup path — the deadline branch already does the
        right sequence, and cancellation joins it instead of inventing a second one.

        The event NAME must match `ingest.workflow.CANCEL_EVENT`; a typo hangs the run until its
        deadline with nothing saying why, which is why both sides read the one constant.
        """
        import dapr.ext.workflow as wf

        from ingest.workflow import CANCEL_EVENT

        wf.DaprWorkflowClient().raise_workflow_event(run_id, CANCEL_EVENT, data={"reason": reason})
        return True

    def pause(self, run_id: str) -> None:
        """Suspend a live run (DWF-MGT-004).

        UNLIKE TERMINATE, this is the SDK call and not an event. Terminate had to become a `cancel`
        event because `terminate_workflow` skips the rest of the generator, and the skipped tail held
        `emit_terminal` — the only caller of `release_run_units`. Pause skips nothing: the instance
        stops being scheduled and resumes exactly where it was, so the tail still runs when it does.

        The use it exists for is holding a fan-out while a credential is rotated — a run that is fine
        but must not proceed for a few minutes. Terminating that run throws away the work it has done.
        """
        import dapr.ext.workflow as wf

        wf.DaprWorkflowClient().pause_workflow(run_id)

    def resume(self, run_id: str) -> None:
        """Resume a suspended run (DWF-MGT-005).

        Shipped in the same change as `pause`, and that pairing is a contract rather than a courtesy:
        a paused instance with no way back is strictly worse than a terminated one, because it holds
        its JetStream subject and its durable consumer while making no progress at all.
        """
        import dapr.ext.workflow as wf

        wf.DaprWorkflowClient().resume_workflow(run_id)


class _DaprWorkflowReader:
    """Reads a run's live state from the engine, for `GET /v1/ingests/{id}`.

    The run's truth lives in the workflow's durable history, not in this process. Reading it here —
    rather than having activities write back into a local cache — is what makes the status endpoint
    correct after a pod restart and consistent across replicas, and it removes the second writable
    copy of a state that already has an owner.
    """

    def state(self, run_id: str) -> dict[str, object] | None:
        try:
            import dapr.ext.workflow as wf

            client = wf.DaprWorkflowClient()
            state = client.get_workflow_state(run_id, fetch_payloads=True)
        except Exception:
            # No sidecar, or the instance is unknown. The caller falls back to the accepted record;
            # a status endpoint that 500s when the engine is unreachable fails at precisely the
            # moment an operator is using it to find out why.
            logger.debug("workflow state unavailable for run %s", run_id, exc_info=True)
            return None
        if state is None:
            return None
        # `to_json()`, verified against dapr-ext-workflow 1.18.3's own source: it returns
        # `runtime_status` already flattened to the enum NAME ("COMPLETED", "RUNNING", …) and
        # `serialized_output` as the JSON string the workflow returned. Reading the attributes
        # directly would work — WorkflowState.__getattr__ proxies to the wrapped object — but
        # `runtime_status` is a property that re-maps to a DIFFERENT enum, so the attribute and the
        # dict disagree on their vocabulary. One accessor, one vocabulary.
        return _with_fanout_progress(client, run_id, dict(state.to_json()))


class _WorkflowStateLike(Protocol):
    """The one method this module reads off a workflow state — see `state`'s note on `to_json()`."""

    def to_json(self) -> dict[str, object]: ...


class _WorkflowStateReader(Protocol):
    """The one method `_with_fanout_progress` needs from the SDK client.

    Protocols rather than `Any`: the helper is handed a real `DaprWorkflowClient` in production and a
    double in tests, and naming exactly what it calls is what keeps the double honest — and what lets
    `ty` narrow the `None` branch below instead of being told to look away.
    """

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _WorkflowStateLike | None: ...


def _as_custom_status(payload: object) -> dict[str, object]:
    """The custom status as a mapping — `{}` for absent, unparseable, or non-object."""
    if not isinstance(payload, str) or not payload:
        return {}
    try:
        loaded = json.loads(payload)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _with_fanout_progress(client: _WorkflowStateReader, run_id: str, payload: dict[str, object]) -> dict[str, object]:
    """Sum the CHILDREN's progress into the parent's status while the fan-out is running.

    `chunk_run` publishes per-chunk progress on its own instance, under a heading calling it "THE
    FAN-OUT'S ONLY PROGRESS SIGNAL" — and nothing read it. The parent's status for the whole fan-out
    is what it set BEFORE dispatch (`units_total`, `chunks`), with no `units_done` key, so
    `GET /v1/ingests/{id}` fell through to the accepted record's `0`. An operator watching a 10M-unit
    harvest read "0 of 10,000,000" for hours on a run landing rows the whole time — which is
    indistinguishable from a wedged run, the state this plane's terminate door exists for.

    Done on the READ side deliberately. Aggregating in the parent means racing a timer against the
    fan-out, which adds actions to `ingest_run`'s stream and so breaks replay for every in-flight
    instance. This costs nothing durable: the child ids are derived exactly as the workflow derives
    them, and the chunk count is already in the parent's own status.

    Four things it does NOT do, each for a reason:
      * not once the run is terminal — `finalize`'s output is then authoritative, and fanning out
        would be N wasted round-trips on every poll of a finished run;
      * not when the parent already carries `units_done` — it sets that itself once the fan-in
        returns, and that aggregate outranks this one;
      * not without a chunk count — before `enumerate_chunks` returns there are no children, and
        guessing an id range costs a round-trip per guess against an engine answering None to each;
      * not partially — a child read that RAISES abandons the whole sum, because an undercount would
        render as progress going backwards, which reads as corruption rather than as a failed read.
    """
    if str(payload.get("runtime_status") or "") != "RUNNING":
        return payload
    custom = _as_custom_status(payload.get("serialized_custom_status"))
    chunks = custom.get("chunks")
    if not isinstance(chunks, int) or chunks <= 0 or "units_done" in custom:
        return payload

    done = 0
    for index in range(chunks):
        try:
            child = client.get_workflow_state(f"{run_id}-c{index}", fetch_payloads=True)
        except Exception:
            logger.debug("child progress unavailable for run %s chunk %d", run_id, index, exc_info=True)
            return payload
        if child is None:
            # Scheduled as a batch, but they appear one at a time. Absent means "not started yet",
            # which is a 0, not an error.
            continue
        reported = _as_custom_status(dict(child.to_json()).get("serialized_custom_status")).get("units_done")
        if isinstance(reported, int):
            done += reported

    return {**payload, "serialized_custom_status": json.dumps({**custom, "units_done": done})}
