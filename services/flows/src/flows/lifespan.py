"""flows service lifespan — the HTTP client, the run store, and the Dapr Workflow runtime.

The runtime starts ONLY when a sidecar is actually there. `DAPR_GRPC_PORT` is the signal: the Dapr
injector sets it in the pod, and nothing sets it in dev. Without that check the service would build a
gRPC worker aimed at a port nobody is listening on, retry it forever in the background, and fill the
log of every local `make dev-micro` run with connection errors about a lane the operator did not ask
for. With it, `make dev-micro` is silent and in-cluster runs are durable — one code path, decided by
the environment that can actually answer the question.

The import of `flows.workflow` is INSIDE that branch on purpose (see `runtime.py`): importing it is
what registers the workflow and its activity, and what pays for grpc.

`DaprFlowScheduler` at the bottom is the other half, and it is deliberately more than one `await`:
its three outcomes (created / unconfirmed / refused) are what let the route degrade to the inline
lane without ever running a graph that the engine is also running. The outcomes are declared on the
seam (`dependencies.FlowScheduler`, with `ScheduleUnconfirmed` beside it); this class's docstring is
why they cannot be collapsed, and `routes.create_run` is the only caller.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI

from flows.config import build_flows_settings
from flows.dependencies import FlowRunReader, FlowScheduler, ScheduleUnconfirmed
from service_kit.config import Settings
from service_kit.governed.actor_state_store import probe_actor_state_store
from service_kit.governed.fga import dispose as fga_dispose


log = logging.getLogger(__name__)

#: How long a schedule call may block before the POST stops WAITING for it. `DaprWorkflowClient` is
#: SYNCHRONOUS gRPC, so an unbounded call would hold the request open on an unreachable sidecar; a
#: caller that never gets an answer cannot retry intelligently. Note what this bound is NOT: it does
#: not stop the call — see `DaprFlowScheduler`.
SCHEDULE_TIMEOUT_SECONDS = 5.0

#: How long the still-running call is then given to SETTLE before the request refuses. Short,
#: because it is only there to catch the common shape of a slow schedule — a sidecar that answered
#: just after the bound — and every second here is a second of a POST nobody can act on.
SETTLE_TIMEOUT_SECONDS = 2.0

#: Bound on the instance-state probe. The probe exists to turn "we stopped waiting" into "the engine
#: says yes/no", so it must not be able to hang for longer than the wait it is adjudicating.
PROBE_TIMEOUT_SECONDS = 2.0


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.flows_settings = build_flows_settings()
        flows_settings = app.state.flows_settings
        # AUTHN/AUTHZ, the viewer's shape: both default OFF and the service behaves exactly as it
        # always did when they are. Built here, never at import (an OIDCVerifier fetches discovery).
        # A failure to BUILD is logged and non-fatal on purpose: the dependency then finds no
        # verifier/client on app.state and answers 503 — falling back to a permissive checker would
        # turn a broken authorization layer into an open one.
        if flows_settings.oidc_enabled and flows_settings.oidc_issuer and flows_settings.oidc_audience:
            try:
                from service_kit.governed.oidc import OIDCVerifier

                app.state.oidc = OIDCVerifier(
                    flows_settings.oidc_issuer,
                    flows_settings.oidc_audience,
                    flows_settings.oidc_cache_ttl,
                    leeway=flows_settings.oidc_leeway,
                    allow_insecure=flows_settings.oidc_allow_insecure,
                    discovery_overrides=({flows_settings.oidc_issuer: flows_settings.oidc_discovery_url} if flows_settings.oidc_discovery_url else None),
                )
                log.info("flows: OIDC verifier ready (issuer=%s)", flows_settings.oidc_issuer)
            except Exception:
                log.exception("flows: OIDC verifier failed to build — the run door will 503")
        if flows_settings.fga_enabled:
            try:
                from service_kit.governed import fga

                store_id, model_id = flows_settings.fga_store_id, flows_settings.fga_model_id
                if not (store_id and model_id):
                    store_id, model_id = await fga.provision(flows_settings.fga_api_url)
                    log.info("flows: openfga provisioned store=%s model=%s", store_id, model_id)
                app.state.fga = fga.make_client(flows_settings.fga_api_url, store_id, model_id, timeout_seconds=flows_settings.fga_timeout_seconds)
                log.info("flows: FGA client ready (%s)", flows_settings.fga_api_url)
            except Exception:
                log.exception("flows: FGA client failed to build — the run door will 503")
        # One app-scoped client so connections are pooled across runs. The per-node budget is passed
        # per call (executor._call_serve); this is the connect/default bound.
        app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=app.state.flows_settings.serve_timeout))
        app.state.runs = {}
        app.state.workflow_runtime = None
        app.state.workflow_scheduler = None
        app.state.workflow_reader = None

        runtime = None
        if os.environ.get("DAPR_GRPC_PORT"):
            try:
                # Importing the module IS the registration (@wfr.workflow / @wfr.activity).
                import flows.workflow  # noqa: F401  — imported for its registration side effect
                from flows.runtime import wfr

                # start() spawns the worker's own threads; it does not block the event loop.
                wfr.start()
                runtime = wfr
                app.state.workflow_runtime = wfr
                app.state.workflow_scheduler = DaprFlowScheduler()
                # Paired with the scheduler on purpose: the durable lane is only durable if it can be
                # READ back, and a lane that schedules without a reader reports every run as
                # "running" forever. They are set together so they cannot drift apart.
                app.state.workflow_reader = DaprFlowRunReader()
                log.info("dapr workflow runtime started — runs are durable")
                # Everything above is PROCESS-LOCAL and cannot fail for a missing actor state
                # store: registration never touches the sidecar, so this reports success while
                # every actor call still refuses. Ask the sidecar what it can actually see.
                await probe_actor_state_store(capability="the studio flow-builder's durable lane cannot run")
            except Exception:
                # Deliberately non-fatal, and the reason is the same one `ingest` records: a service
                # that refuses to start because its sidecar is not up YET turns an ordering blip into
                # a CrashLoopBackOff. Runs fall back to the inline lane, which works.
                log.warning("dapr workflow runtime unavailable — runs execute inline", exc_info=True)
        else:
            # Once per process, at INFO: the absence of the durable lane is a fact about the
            # deployment, not a problem, and it is the first thing to check when a run is not durable.
            log.info("no dapr sidecar (DAPR_GRPC_PORT unset) — runs execute inline")

        log.info("startup_complete")
        try:
            yield
        finally:
            # The OpenFGA client this lifespan opened — aiohttp-backed, so collected unclosed it leaves
            # one half-open connection per replica until OpenFGA's idle timeout. Disposal lives beside
            # the factory rather than as another copy of the same block.
            await fga_dispose(app)
            # The client is closed FIRST and unconditionally. `WorkflowRuntime.shutdown()` talks to the
            # sidecar, so it CAN raise — and if it does while it runs first, every pooled connection
            # leaks on the way out. A release must not sit behind an operation that can fail.
            await app.state.http.aclose()
            if runtime is not None:
                try:
                    runtime.shutdown()
                except Exception:
                    log.warning("workflow runtime shutdown failed", exc_info=True)
            log.info("shutdown_complete")

    return lifespan


class DaprFlowScheduler(FlowScheduler):
    """Schedules `flow_run` through the Dapr workflow client in the sidecar.

    The client is imported and constructed per call, not held: it is a gRPC client bound to the
    sidecar's address, and one captured at startup outlives the sidecar it was built for across a
    pod's lifetime.

    **Why this is not one `await`.** `asyncio.wait_for` cancels the WAIT, never the work:
    `asyncio.to_thread` hands the call to an executor with no cancellation channel, so a synchronous
    `schedule_new_workflow` keeps running to completion after the bound fires. A slow-but-SUCCESSFUL
    schedule therefore raised `TimeoutError` into a caller that read it as "the durable lane is
    unavailable" and ran the same graph inline — the instance existed, so the run executed twice, and
    the caller was told about the inline one. A timeout is not evidence of anything; only the engine
    is. Hence the three outcomes, which are this class's contract with `routes.create_run`:

    * **returns** — the instance EXISTS. Nothing may run inline.
    * **`ScheduleUnconfirmed`** — unknown, and only a retry can resolve it. Refuse (503).
    * **any other exception** — the engine did not create it AND a state probe agreed. This is the
      path that keeps the deliberate degrade-to-inline alive (routes.create_run's incident comment).
    """

    async def schedule(self, run_id: str, payload: dict[str, object]) -> None:
        # A task, not a bare `to_thread` coroutine, so the call outlives the wait and can be ASKED
        # what happened instead of guessed about.
        call = asyncio.create_task(asyncio.to_thread(self._create, run_id, payload))
        try:
            # shield: without it `wait_for` cancels `call`, which stops us looking without stopping
            # the gRPC call — the exact ambiguity this method exists to remove.
            await asyncio.wait_for(asyncio.shield(call), timeout=SCHEDULE_TIMEOUT_SECONDS)
        except TimeoutError:
            await self._settle(run_id, call)
        except Exception:
            # An error is not proof of non-creation (a deadline or a broken stream can arrive after
            # the engine committed the instance), and the duplicate-instance refusal a retried POST
            # earns is not a failure at all. Ask the engine before telling the route it may degrade.
            if await self._instance_exists(run_id):
                log.warning("schedule for %s errored but the instance exists — the run is durable, not inline", run_id)
                return
            raise
        finally:
            # Whatever we decided, a call still in flight has an outcome nobody is waiting for. The
            # callback logs it and retrieves its exception, which the loop would otherwise report as
            # "never retrieved" long after the request that made it is gone.
            if not call.done():
                call.add_done_callback(_log_late_schedule)

    async def _settle(self, run_id: str, call: asyncio.Task[None]) -> None:
        """Resolve a schedule whose call outlived its bound. Still running is not the same as failed."""
        done, _ = await asyncio.wait({call}, timeout=SETTLE_TIMEOUT_SECONDS)
        if done:
            error = call.exception()
            if error is None:
                log.warning("schedule for %s landed after %.1fs — the run is durable, not inline", run_id, SCHEDULE_TIMEOUT_SECONDS)
                return
            if await self._instance_exists(run_id):
                return
            raise error
        # Still in flight, so the instance may appear a moment from now. A probe that says "absent"
        # is worth nothing here, and the wrong guess runs the graph twice — refuse instead.
        if await self._instance_exists(run_id):
            log.warning("schedule for %s is still in flight but the instance exists — the run is durable", run_id)
            return
        waited = SCHEDULE_TIMEOUT_SECONDS + SETTLE_TIMEOUT_SECONDS
        raise ScheduleUnconfirmed(f"the workflow engine neither started nor refused run {run_id} within {waited:.0f}s")

    async def _instance_exists(self, run_id: str) -> bool:
        """Ask the ENGINE whether the instance is there — the only authority on the question.

        A probe that cannot reach the sidecar answers False, which is the same evidence the schedule
        failure just gave (no engine, nothing created) and is what keeps the degrade-to-inline lane
        working on a sidecar that is not answering. The one place a False would be dangerous — a call
        still in flight — refuses rather than degrades (see `_settle`).
        """
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._exists, run_id), timeout=PROBE_TIMEOUT_SECONDS)
        except Exception:
            log.warning("could not read workflow state for %s — treating the instance as absent", run_id, exc_info=True)
            return False

    def _create(self, run_id: str, payload: dict[str, object]) -> None:
        """The blocking gRPC create.

        Its own method, like `_exists`, so the policy above is exercisable without a sidecar: a test
        subclass substitutes these two and the ordering — the part that was wrong — runs for real.
        """
        import dapr.ext.workflow as wf

        from flows.workflow import flow_run_workflow

        wf.DaprWorkflowClient().schedule_new_workflow(
            workflow=flow_run_workflow,
            input=payload,
            # The run id IS the instance id, so `GET /flows/runs/{id}` and the engine agree on one
            # name for one run — and a retried POST carrying the same `Idempotency-Key` derives the
            # same id, which the engine refuses as a duplicate instead of starting a second run.
            instance_id=run_id,
        )

    def _exists(self, run_id: str) -> bool:
        """The blocking state read. `get_workflow_state` answers None for an instance the engine has
        never heard of, which is exactly the question being asked."""
        import dapr.ext.workflow as wf

        return wf.DaprWorkflowClient().get_workflow_state(run_id, fetch_payloads=False) is not None


class DaprFlowRunReader(FlowRunReader):
    """Reads a run's live state from the sidecar's workflow API.

    Sync, and deliberately: `DaprWorkflowClient` is synchronous gRPC. The route calls it through
    `asyncio.to_thread` rather than inline — a blocking gRPC call in an `async def` stalls the event
    loop for every other request on the worker — which is the same way `ingest.api` drives its own
    reader.
    """

    def state(self, run_id: str) -> dict[str, object] | None:
        try:
            import dapr.ext.workflow as wf

            state = wf.DaprWorkflowClient().get_workflow_state(run_id, fetch_payloads=True)
        except Exception:
            # No sidecar, or the instance is unknown. The caller falls back to the local record: a
            # status endpoint that 500s when the engine is unreachable fails at exactly the moment an
            # operator is using it to find out why. Same reasoning as `ingest.__init__`'s reader.
            log.debug("workflow state unavailable for run %s", run_id, exc_info=True)
            return None
        if state is None:
            return None
        # `to_json()` rather than the attributes, for the reason `ingest` documents against
        # dapr-ext-workflow 1.18.3's source: the dict carries `runtime_status` flattened to the enum
        # NAME ("COMPLETED", "RUNNING", …), while the `runtime_status` PROPERTY re-maps to a different
        # enum. The two disagree on their vocabulary; one accessor keeps the mapping below honest.
        return dict(state.to_json())

    def terminate(self, run_id: str) -> None:
        """Ask the engine to stop scheduling this run's remaining work.

        A HARD terminate is correct HERE, and deliberately not the shape ingest needed. ingest's
        terminate had to become a cancellation EVENT because `terminate_workflow` never resumes the
        generator, so its `emit_terminal` — the only caller of `release_run_units` — never ran and the
        run's JetStream consumer was stranded. `flow_run_workflow` holds no queue, no consumer and no
        external resource: it fans out activities and returns. There is nothing for a skipped
        finally-path to leak, so the simple call is the right one.

        Not idempotent-checked: the SDK answers an unknown or already-terminal instance without
        raising, and the route reports acceptance rather than an outcome.
        """
        import dapr.ext.workflow as wf

        wf.DaprWorkflowClient().terminate_workflow(run_id)


def _log_late_schedule(call: asyncio.Task[None]) -> None:
    """Drain a schedule call the request stopped waiting for, so its outcome reaches the log."""
    if call.cancelled():
        return
    error = call.exception()
    if error is None:
        log.warning("a schedule call the request gave up on SUCCEEDED — that run is durable")
    else:
        log.warning("a schedule call the request gave up on failed: %r", error)
