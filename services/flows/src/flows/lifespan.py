"""flows service lifespan — the HTTP client, the run store, and the Dapr Workflow runtime.

The runtime starts ONLY when a sidecar is actually there. `DAPR_GRPC_PORT` is the signal: the Dapr
injector sets it in the pod, and nothing sets it in dev. Without that check the service would build a
gRPC worker aimed at a port nobody is listening on, retry it forever in the background, and fill the
log of every local `make dev-micro` run with connection errors about a lane the operator did not ask
for. With it, `make dev-micro` is silent and in-cluster runs are durable — one code path, decided by
the environment that can actually answer the question.

The import of `flows.workflow` is INSIDE that branch on purpose (see `runtime.py`): importing it is
what registers the workflow and its activity, and what pays for grpc.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI

from flows.config import build_flows_settings
from flows.dependencies import FlowScheduler
from service_kit.config import Settings


log = logging.getLogger(__name__)

#: How long a schedule call may block before the POST gives up. `DaprWorkflowClient` is SYNCHRONOUS
#: gRPC, so an unbounded call would hold the request open on an unreachable sidecar; a caller that
#: never gets an answer cannot retry intelligently.
SCHEDULE_TIMEOUT_SECONDS = 5.0


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
                log.info("dapr workflow runtime started — runs are durable")
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
    """

    async def schedule(self, run_id: str, payload: dict[str, object]) -> None:
        import dapr.ext.workflow as wf

        from flows.workflow import flow_run_workflow

        def _schedule() -> None:
            wf.DaprWorkflowClient().schedule_new_workflow(
                workflow=flow_run_workflow,
                input=payload,
                # The run id IS the instance id, so `GET /flows/runs/{id}` and the engine agree on
                # one name for one run — and a retried POST with the same id cannot start a second.
                instance_id=run_id,
            )

        # to_thread + a timeout: the client is synchronous gRPC, so calling it on the event loop
        # would block every other request for the duration.
        await asyncio.wait_for(asyncio.to_thread(_schedule), timeout=SCHEDULE_TIMEOUT_SECONDS)
