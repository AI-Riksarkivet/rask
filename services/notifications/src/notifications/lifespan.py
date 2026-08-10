"""The notifications lifespan — the auth clients and the actor plane, built once onto `app.state`.

**Actor registration happens HERE, never at import.** `register_actor` mutates process-global runtime
state (`ActorRuntime._actor_managers`, and the entity list `/dapr/config` advertises), and a side
effect like that must not happen merely because something imported this module for `--help`, for an
image build, or to collect a test.

A failure to register is logged and left NON-FATAL, and that is only survivable because something
actually reads the flag: `dependencies.require_actor_plane` gates every inbox route on it with a 503
carrying the reason, and `/readyz` reports it as a component of a 200. A flag nobody consults is the
defect this service is deliberately not repeating.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Final

from dapr.ext.fastapi import DaprActor
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from notifications.config import get_notifications_settings
from notifications.inbox_actor import InboxActor
from service_kit.config import Settings
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.schemas.health import Readiness, ReadinessStatus


log = logging.getLogger(__name__)


def build_actor_host(app: FastAPI) -> DaprActor | None:
    """Mount the actor HTTP surface the sidecar calls back on (`/dapr/config`, `/actors/...`).

    Constructing `DaprActor` only adds routes — it performs no I/O — so it is safe at import, and it
    has to happen there: routes added after startup are not served. The REGISTRATION that talks to the
    runtime happens in the lifespan below.
    """
    try:
        actor = DaprActor(app)
    except Exception:
        log.exception("notifications: could not mount the actor routes — the inbox will 503")
        return None
    app.add_middleware(BaseHTTPMiddleware, dispatch=_guard_actor_routes)
    return actor


#: What `DaprActor` mounts at the ROOT of this app — outside `RASK_API_PREFIX`, and therefore outside
#: every dependency the inbox router declares.
_ACTOR_PATH_PREFIXES: Final = ("/actors", "/dapr/config")


async def _guard_actor_routes(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Require the sidecar's own token on the actor callback surface.

    **The hole this closes.** `DaprActor(app)` root-mounts
    `PUT /actors/{actor_type}/{actor_id}/method/{method}`, and the actor IS this service's private
    store — one inbox per subject, keyed by an id that is merely `base64url(sub)`. Unguarded, anything
    able to reach `:8850` reads any subject's inbox BY NAME, with no bearer and neither FGA check:

        curl -XPUT :8850/actors/InboxActor/YWxpY2U/method/Page -d '{"state":"all","limit":100}'

    `YWxpY2U` is `base64url("alice")`, and subjects are readable off lineage's author facets — the
    actor id is not a secret and was never meant to be one. The inbox's own doors hang on the ROUTER,
    under `settings.api_prefix`; these routes are mounted at the root and inherit none of them.

    A middleware rather than a router dependency because the SDK adds these routes to the app itself:
    there is no `include_router` call to hang `dependencies=` on, and rewriting a mounted route's
    dependant after the fact is far more fragile than matching a path prefix.

    `require_dapr_token` is the right door, not a stand-in. These paths are BY CONSTRUCTION
    sidecar-delivered — daprd calls them back, presenting `dapr-api-token` when `APP_API_TOKEN` is
    set — and the same dependency refuses a public front door outright, which is the half that still
    holds in dev where the token is unset. It proves "arrived via Dapr", never "trusted caller", and
    that is exactly the claim being made here.

    The annotator mounts `DaprActor(app)` the same way and is unguarded today. That is FILED rather
    than fixed from here — this service is not the place to change the annotator's posture — but a new
    service whose entire purpose is per-subject privacy should not inherit the gap.
    """
    if request.url.path.startswith(_ACTOR_PATH_PREFIXES):
        try:
            require_dapr_token(
                dapr_api_token=request.headers.get("dapr-api-token"),
                dapr_caller_app_id=request.headers.get("dapr-caller-app-id"),
            )
        except HTTPException as refusal:
            return JSONResponse({"detail": refusal.detail}, status_code=refusal.status_code)
    return await call_next(request)


async def actor_plane_ready(request: Request) -> Readiness:
    """Report the actor plane as a COMPONENT of a 200, never as `degraded`.

    `service_kit.probes` renders `degraded` as a 503, which would pull the pod from rotation. Refusing
    the inbox is `require_actor_plane`'s job; this probe's job is to report, not to act — and a service
    whose health surface and gateway row still answer is a service an operator can diagnose.
    """
    registered = bool(getattr(request.app.state, "actors_registered", False))
    return Readiness(status=ReadinessStatus.ready, components={"actors": "registered" if registered else "unregistered"})


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        # The SAME cached instance the InboxActor reads: an actor is constructed by the Dapr runtime and
        # cannot be handed a dependency, so `get_notifications_settings` is its only route to config.
        # Putting that one object on app.state keeps the routes on DI without a second construction.
        app.state.notifications_settings = get_notifications_settings()
        notifications_settings = app.state.notifications_settings

        # Authn/authz, the fleet's shape: both default OFF and the service behaves as it would without
        # them. Built here, never at import (an OIDCVerifier fetches discovery). A failure to BUILD is
        # logged and non-fatal on purpose: the dependency then finds no verifier/client on app.state and
        # answers 503, where falling back to a permissive checker would turn a broken authorization
        # layer into an open one — on a plane whose whole point is that a badge counts YOUR work.
        auth = notifications_settings
        if auth.oidc_enabled and auth.oidc_issuer and auth.oidc_audience:
            try:
                # Imported here, not at module scope: constructing a verifier fetches discovery/JWKS.
                from service_kit.governed.oidc import OIDCVerifier

                app.state.oidc = OIDCVerifier(
                    auth.oidc_issuer,
                    auth.oidc_audience,
                    auth.oidc_cache_ttl,
                    leeway=auth.oidc_leeway,
                    allow_insecure=auth.oidc_allow_insecure,
                    # Split-horizon (reverse-proxied IdP): fetch discovery in-cluster while tokens keep
                    # the public issuer string. Same wiring as the catalog and annotator, deliberately.
                    discovery_overrides=({auth.oidc_issuer: auth.oidc_discovery_url} if auth.oidc_discovery_url else None),
                )
                log.info("notifications: OIDC verifier ready (issuer=%s)", auth.oidc_issuer)
            except Exception:
                log.exception("notifications: OIDC verifier failed to build — the inbox will 503")
        if auth.fga_enabled:
            try:
                from service_kit.governed import fga

                store_id, model_id = auth.fga_store_id, auth.fga_model_id
                if not (store_id and model_id):
                    store_id, model_id = await fga.provision(auth.fga_api_url)
                    log.info("notifications: openfga provisioned store=%s model=%s", store_id, model_id)
                app.state.fga = fga.make_client(auth.fga_api_url, store_id, model_id, timeout_seconds=auth.fga_timeout_seconds)
                log.info("notifications: FGA client ready (%s)", auth.fga_api_url)
            except Exception:
                log.exception("notifications: FGA client failed to build — the inbox will 503")

        actor_ext: DaprActor | None = getattr(app.state, "actor_ext", None)
        if actor_ext is not None:
            try:
                await actor_ext.register_actor(InboxActor)
                app.state.actors_registered = True
                log.info("notifications: InboxActor registered")
            except Exception:
                app.state.actors_registered = False
                log.exception("notifications: actor registration failed — the inbox will 503")

        app.state.startup_complete = True
        app.state.shutting_down = False
        try:
            yield
        finally:
            app.state.shutting_down = True
            # The client the lifespan built is the lifespan's to release — the same shape `lineage` and
            # `catalog` already carry. Without it the SDK's aiohttp session is collected unclosed, so a
            # rolling restart leaves one half-open connection per replica on OpenFGA until its own idle
            # timeout, and the only trace is an "Unclosed client session" line on the way out.
            # Suppressed rather than raised: a shutdown path that raises hides whatever came after it,
            # and there is nothing a failed close can be retried against on a pod that is leaving.
            fga_client = getattr(app.state, "fga", None)
            if fga_client is not None:
                with suppress(Exception):
                    await fga_client.close()

    return lifespan
