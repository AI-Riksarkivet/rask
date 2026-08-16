"""Annotator service entry — the lance-ns thin-``main.py`` template.

Module-level ``app``; ALL construction in ``lifespan`` onto ``app.state``
(importing this module does zero I/O). Problem+json handlers, CORS middleware,
``/livez`` + ``/readyz`` gated on ``startup_complete``/``shutting_down``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from dapr.aio.clients import DaprClient
from dapr.ext.fastapi import DaprActor
from fastapi import FastAPI, Request

from annotator.api.v1.router import router as api_router
from annotator.core.config import get_annotator_settings
from annotator.projects.actor import AnnotationTaskActor
from annotator.projects.project_actor import AnnotationProjectActor
from annotator.projects.tenant_actor import TenantProjectsActor
from service_kit.control_emit import make_control_emitter, set_process_control_emitter
from service_kit.exceptions import register_handlers
from service_kit.governed import fga
from service_kit.governed.actor_warmup import warm_actor_proxy_factory
from service_kit.governed.dapr_auth import guard_actor_routes
from service_kit.governed.oidc import OIDCVerifier
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.media.middleware import register_middleware
from service_kit.media.state import AppState, dataset_handle
from service_kit.obs import configure_app_logging
from service_kit.probes import make_probes_router
from service_kit.schemas.health import Readiness, ReadinessStatus


logger = logging.getLogger(__name__)

configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_annotator_settings()
    state = AppState(settings=settings, http=httpx.Client())
    app.state.resources = state
    try:
        handle = dataset_handle(state)  # fail-fast open of the default descriptor
        logger.info("annotator: default dataset %s ready (%d tables)", handle.id, len(handle.descriptor.tables))
    except Exception:
        # /livez stays green; per-request resolution surfaces the problem as a domain 404.
        logger.exception("annotator: default dataset failed to open — serving degraded")

    # Governed auth. Both are built here and read per-request off app.state; when either is enabled
    # but absent, `annotator.api.security` raises 503 rather than degrading to open access. A failure
    # is logged and the attribute left UNSET on purpose — a half-built auth layer must not look
    # configured, and 503 is the honest answer until it is fixed.
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_audience:
        try:
            app.state.oidc = OIDCVerifier(
                settings.oidc_issuer,
                settings.oidc_audience,
                settings.oidc_cache_ttl,
                leeway=settings.oidc_leeway,
                allow_insecure=settings.oidc_allow_insecure,
                # Split-horizon (reverse-proxied IdP): fetch discovery/JWKS in-cluster while tokens
                # keep the public issuer string. Same wiring as the catalog, deliberately.
                discovery_overrides=({settings.oidc_issuer: settings.oidc_discovery_url} if settings.oidc_discovery_url else None),
            )
            logger.info("annotator: OIDC verifier ready (issuer=%s)", settings.oidc_issuer)
        except Exception:
            logger.exception("annotator: OIDC verifier failed to build — guarded routes will 503")
    if settings.fga_enabled:
        try:
            store_id, model_id = settings.fga_store_id, settings.fga_model_id
            if not (store_id and model_id):
                store_id, model_id = await fga.provision(settings.fga_api_url)
                logger.info("annotator: openfga provisioned store=%s model=%s", store_id, model_id)
            app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
            logger.info("annotator: FGA client ready (%s)", settings.fga_api_url)
        except Exception:
            logger.exception("annotator: FGA client failed to build — authorized routes will 503")

    # Control-plane change events. Built here so the emit path is a pure in-process call on the request
    # path: the Dapr client targets the local sidecar, so construction needs no broker reachability.
    # A build failure leaves the no-op in place rather than the attribute unset — unlike auth above,
    # a missing notification must never 503 a task transition.
    control_dapr: DaprClient | None = None
    if settings.control_emit_enabled:
        try:
            control_dapr = DaprClient()
        except Exception:
            logger.exception("annotator: Dapr client failed to build — task assignments will not notify")
    app.state.control_emitter = make_control_emitter(
        enabled=settings.control_emit_enabled,
        dapr=control_dapr,
        pubsub=settings.control_pubsub,
        timeout_seconds=settings.control_emit_timeout_seconds,
        service="annotator",
    )
    # ALSO publish it process-wide, for the one producer that has no request to resolve a dependency
    # from: the task actor's lease reminder. Without this line a lapsed lease emits through the no-op
    # and the annotator who lost their hold is told nothing — the same silence, arriving by a subtler
    # door than a missing emit.
    set_process_control_emitter(app.state.control_emitter)

    # Register the actor types. This is the estate's FIRST actor: `lance-statestore` has carried
    # `actorStateStore: "true"` scoped to catalog+annotator since it was provisioned, and nothing used
    # it. Registration happens in the LIFESPAN, not at import, because it mutates process-global
    # runtime state (`ActorRuntime._actor_managers`, and the entity list `/dapr/config` advertises) —
    # a side effect that merely importing this module for `--help` or an image build must not have.
    #
    # **What it proves is LOCAL, and the flag below must not be read as more.** `register_actor`
    # builds the type info, constructs an actor client WITHOUT invoking it, and stores an
    # `ActorManager` in a dict; daprd learns the entity list afterwards by POLLING `/dapr/config`.
    # Nothing here talks to the sidecar, so this block raises only for an actor CLASS this service
    # cannot register — never because daprd is absent, which is why `actors_registered` is `True` in a
    # no-sidecar composition (dev-micro, the e2e-py harness) and the task plane there fails per
    # request exactly as it did before.
    #
    # A failure here is logged and left non-fatal: the read-plane annotation routes do not need
    # actors, so a broken task plane must not take the media surface down with it. `actors_registered`
    # is what makes that survivable rather than merely quiet — `tasks.require_actor_plane` gates every
    # task route on it (503, with the reason), and `/readyz` reports it as a component.
    if actor_ext is not None:
        try:
            await actor_ext.register_actor(AnnotationTaskActor)
            await actor_ext.register_actor(AnnotationProjectActor)
            await actor_ext.register_actor(TenantProjectsActor)
            app.state.actors_registered = True
            logger.info("annotator: AnnotationTaskActor + AnnotationProjectActor + TenantProjectsActor registered with the sidecar")
            # The SDK's proxy factory runs a BLOCKING `wait_for_sidecar()` on first use (60 s default,
            # a `time.sleep` loop), so whichever request opens the first actor proxy pays it ON THE
            # EVENT LOOP. Measured in the notifications service, where it made a wall-clock budget
            # unenforceable; this plane has the same call shape, so it gets the same warm-up.
            await warm_actor_proxy_factory()
        except Exception:
            app.state.actors_registered = False
            logger.exception("annotator: actor registration failed — the task plane will 503")

    app.state.startup_complete = True
    app.state.shutting_down = False
    yield
    app.state.shutting_down = True
    for resource in (state.http, state.embedder, state.reranker):
        close = getattr(resource, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                logger.warning("error closing %s on shutdown", type(resource).__name__)


app = FastAPI(title="lance-media annotator", lifespan=lifespan)
register_handlers(app)
# `register_handlers` maps `DomainError` only. The OIDC verifier raises `lance_namespace`'s
# `UnauthenticatedError`, which is a `LanceNamespaceError` and NOT a `DomainError` — so an expired or
# wrong-audience bearer escaped unmapped and FastAPI answered 500. The zone renders that as "The
# annotation service is unreachable", which sends everyone to look at networking and the deployment
# for what is really "sign in again". Measured live: `security.py:55 authenticate` ->
# `oidc.py:260 verify` -> raise -> 500 on a request from a signed-in user.
#
# This is the SAME installer the catalog has used since the merge (`catalog/main.py`); it was simply
# never added to the three media services, all of which authenticate the same way.
install_problem_handlers(app, logger)
register_middleware(app, get_annotator_settings())

# Mount the actor HTTP surface the sidecar calls back on (/dapr/config, /actors/...). Constructing
# DaprActor only adds routes — it performs no I/O — so it is safe at import; the REGISTRATION that
# does talk to the sidecar happens in the lifespan above. Built here rather than inside the lifespan
# because routes added after startup are not served.
actor_ext: DaprActor | None
try:
    actor_ext = DaprActor(app)
except Exception:  # pragma: no cover - a broken ext must not take the read plane down
    actor_ext = None
    logger.exception("annotator: could not mount the actor routes — the task plane is unavailable")
else:
    # The actor callback surface is SIDECAR-ONLY, and it was open. `DaprActor` mounts
    # `PUT /actors/{type}/{id}/method/{method}` at the ROOT, so it inherits none of the doors the task
    # and project routers declare — and these actors hold the task plane's own state: claims, drafts,
    # reviews, the lease holder. The actor id is a project id, which is not a secret and never was.
    # Same guard, same reason, one implementation (`service_kit.governed.dapr_auth`) as notifications.
    guard_actor_routes(app)

#: The task plane's own health, defined HERE and not only in the lifespan so it is never merely
#: absent: a mount that failed above never reaches the registration block, and a flag that only
#: exists on the happy path cannot be the thing a route gates on. False until the lifespan proves
#: otherwise — the honest default, since nothing is registered yet at import.
app.state.actors_registered = False


async def _actor_plane_ready(request: Request) -> Readiness:
    """Report the actor plane as a COMPONENT of a 200, never as `degraded`.

    `service_kit.probes` renders `degraded` as a 503, which would pull the pod from rotation and take
    the read-plane annotation routes down with the task plane — the exact coupling the non-fatal
    registration in the lifespan exists to avoid. Refusing the task plane is the task routes' job
    (`tasks.require_actor_plane`); this probe's job is to report, not to act.

    **`registered` is not a health check on the sidecar.** The flag records only that this process
    could register its actor CLASSES (see the lifespan) — a daprd that is absent, unreachable or not
    placing actors reports `registered` here and still fails every task invocation. Reporting THAT
    means probing daprd live on each `/readyz`, which couples this pod's readiness to its sidecar and
    is a deliberate decision nobody has taken; until it is, read this component as "the process's own
    actor registration", not as "the task plane works".
    """
    registered = bool(getattr(request.app.state, "actors_registered", False))
    return Readiness(status=ReadinessStatus.ready, components={"actors": "registered" if registered else "unregistered"})


# /livez + /readyz — the shared router (service_kit.probes), not a hand-rolled copy.
app.include_router(make_probes_router(_actor_plane_ready))
app.include_router(api_router)


def run() -> None:
    import uvicorn

    s = get_annotator_settings()
    uvicorn.run("annotator.main:app", host=s.host, port=s.service_port)
