"""Annotator service entry — the lance-ns thin-``main.py`` template.

Module-level ``app``; ALL construction in the lifespan onto ``app.state`` (importing this module does
zero I/O). The lifespan and the assembly are both SHARED — ``service_kit.media.lifespan`` and
``service_kit.media.app`` — because viewer, search and annotator are three deployments of one shape
and used to hand-write it three times (docs/DECISIONS.md "The Python estate audit" DUP-16 / X12 / DUP-20). What is genuinely
this service's own — the control emitter and the actor plane — arrives through the shared lifespan's
``setup``/``teardown`` hooks, which run before readiness is announced and before anything shared is
disposed.
"""

from __future__ import annotations

import logging
from contextlib import suppress

from dapr.aio.clients import DaprClient
from dapr.ext.fastapi import DaprActor
from fastapi import FastAPI, Request

from annotator.api.v1.router import router as api_router
from annotator.core.config import get_annotator_settings
from annotator.projects.actor import AnnotationTaskActor
from annotator.projects.project_actor import AnnotationProjectActor
from annotator.projects.tenant_actor import TenantProjectsActor
from service_kit import setup_logging
from service_kit.control_emit import make_control_emitter, set_process_control_emitter
from service_kit.governed.actor_state_store import probe_actor_state_store
from service_kit.governed.actor_warmup import warm_actor_proxy_factory
from service_kit.governed.dapr_auth import guard_actor_routes
from service_kit.media.app import build_media_app
from service_kit.media.config import MediaSettings
from service_kit.media.lifespan import make_media_lifespan
from service_kit.media.state import AppState
from service_kit.obs import configure_app_logging
from service_kit.schemas.health import Readiness, ReadinessStatus


logger = logging.getLogger(__name__)

configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)


async def _start_task_plane(app: FastAPI, _state: AppState) -> None:
    """The annotator's own startup, run BEFORE readiness is announced.

    Ordering is the whole reason this is a hook and not code after the shared lifespan: a pod that
    reports ready while its task actors are unregistered answers 503 to every task route.

    **Control-plane change events.** Built here so the emit path is a pure in-process call on the
    request path: the Dapr client targets the local sidecar, so construction needs no broker
    reachability. A build failure leaves the no-op emitter in place rather than the attribute unset —
    unlike auth, a missing notification must never 503 a task transition. It is ALSO published
    process-wide, for the one producer that has no request to resolve a dependency from: the task
    actor's lease reminder. Without that line a lapsed lease emits through the no-op and the
    annotator who lost their hold is told nothing — the same silence by a subtler door.

    **Actor registration** happens here, not at import, because it mutates process-global runtime
    state (`ActorRuntime._actor_managers`, and the entity list `/dapr/config` advertises) — a side
    effect that merely importing this module for `--help` or an image build must not have.

    **What it proves is LOCAL, and `actors_registered` must not be read as more.** `register_actor`
    builds the type info, constructs an actor client WITHOUT invoking it, and stores an
    `ActorManager` in a dict; daprd learns the entity list afterwards by POLLING `/dapr/config`.
    Nothing here talks to the sidecar, so this block raises only for an actor CLASS this service
    cannot register — never because daprd is absent, which is why the flag is `True` in a no-sidecar
    composition (dev-micro, the e2e-py harness) and the task plane there fails per request exactly as
    it did before. A failure is logged and left non-fatal: the read-plane annotation routes do not
    need actors, so a broken task plane must not take the media surface down with it.
    `tasks.require_actor_plane` gates every task route on the flag (503, with the reason), and
    `/readyz` reports it as a component.
    """
    settings = get_annotator_settings()
    control_dapr: DaprClient | None = None
    if settings.control_emit_enabled:
        try:
            control_dapr = DaprClient()
        except Exception:
            logger.exception("annotator: Dapr client failed to build — task assignments will not notify")
    app.state.control_dapr = control_dapr
    app.state.control_emitter = make_control_emitter(
        enabled=settings.control_emit_enabled,
        dapr=control_dapr,
        pubsub=settings.control_pubsub,
        timeout_seconds=settings.control_emit_timeout_seconds,
        service="annotator",
    )
    set_process_control_emitter(app.state.control_emitter)

    if actor_ext is None:
        return
    try:
        await actor_ext.register_actor(AnnotationTaskActor)
        await actor_ext.register_actor(AnnotationProjectActor)
        await actor_ext.register_actor(TenantProjectsActor)
        app.state.actors_registered = True
        logger.info("annotator: AnnotationTaskActor + AnnotationProjectActor + TenantProjectsActor registered with the sidecar")
        # The SDK's proxy factory runs a BLOCKING `wait_for_sidecar()` on first use (60 s default, a
        # `time.sleep` loop), so whichever request opens the first actor proxy pays it ON THE EVENT
        # LOOP. Measured in the notifications service, where it made a wall-clock budget
        # unenforceable; this plane has the same call shape, so it gets the same warm-up.
        await warm_actor_proxy_factory()
        # Everything above is PROCESS-LOCAL and cannot fail for a missing actor state store:
        # registration never touches the sidecar, so this reports success while every actor call
        # still refuses. Ask the sidecar what it can actually see.
        await probe_actor_state_store(capability="the annotation task plane cannot hold a task")
    except Exception:
        app.state.actors_registered = False
        logger.exception("annotator: actor registration failed — the task plane will 503")


async def _stop_task_plane(app: FastAPI) -> None:
    """Close the control emitter's Dapr channel — a `grpc.aio` channel built in startup and, before
    this hook existed, closed nowhere, so a rolling restart leaked one per replica alongside the FGA
    session. Suppressed like every other close: a shutdown that raises hides whatever came after it."""
    control_dapr = getattr(app.state, "control_dapr", None)
    if control_dapr is not None:
        with suppress(Exception):
            await control_dapr.close()


# Application logging, before the app exists — every module here uses getLogger(__name__), and
# without this they propagate to a root logger with no handlers and are DISCARDED
# (see service_kit.setup_logging).
setup_logging()


async def _actor_plane_ready(request: Request) -> Readiness:
    """Report the actor plane as a COMPONENT of a 200, never as `degraded`.

    `service_kit.probes` renders `degraded` as a 503, which would pull the pod from rotation and take
    the read-plane annotation routes down with the task plane — the exact coupling the non-fatal
    registration in `_start_task_plane` exists to avoid. Refusing the task plane is the task routes'
    job (`tasks.require_actor_plane`); this probe's job is to report, not to act.

    **`registered` is not a health check on the sidecar.** The flag records only that this process
    could register its actor CLASSES — a daprd that is absent, unreachable or not placing actors
    reports `registered` here and still fails every task invocation. Reporting THAT means probing
    daprd live on each `/readyz`, which couples this pod's readiness to its sidecar and is a decision
    nobody has taken; until it is, read this component as "the process's own actor registration", not
    as "the task plane works".
    """
    registered = bool(getattr(request.app.state, "actors_registered", False))
    return Readiness(status=ReadinessStatus.ready, components={"actors": "registered" if registered else "unregistered"})


#: `closes` names the resources THIS service opens. Only the http pool: `AppState` carries slots for
#: every media service (the search service's model handles among them), and closing those here would
#: be a no-op that reads as ownership this service does not have.
def _settings() -> MediaSettings:
    """Read the settings AT LIFESPAN TIME, never at import.

    A named indirection rather than passing `get_annotator_settings` itself: resolving the module global on
    each call is what lets a test drive the real lifespan against its own settings, and what keeps
    the process's configuration from being decided by this module's import order.
    """
    return get_annotator_settings()


lifespan = make_media_lifespan(
    _settings,
    service="annotator",
    closes=lambda state: (state.http,),
    setup=_start_task_plane,
    teardown=_stop_task_plane,
)

app = build_media_app(
    title="lance-media annotator",
    settings=get_annotator_settings(),
    routers=[api_router],
    lifespan=lifespan,
    ready_check=_actor_plane_ready,
)

# Mount the actor HTTP surface the sidecar calls back on (/dapr/config, /actors/...). Constructing
# DaprActor only adds routes — it performs no I/O — so it is safe at import; the REGISTRATION that
# does talk to the sidecar happens in `_start_task_plane`. Built here rather than inside the lifespan
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
#: exists on the happy path cannot be the thing a route gates on. False until `_start_task_plane`
#: proves otherwise — the honest default, since nothing is registered yet at import.
app.state.actors_registered = False


def run() -> None:
    import uvicorn

    s = get_annotator_settings()
    uvicorn.run("annotator.main:app", host=s.host, port=s.service_port)
