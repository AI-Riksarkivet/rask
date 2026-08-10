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
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress

import httpx
from dapr.ext.fastapi import DaprActor
from fastapi import FastAPI, Request

from notifications.api.reconciler import LineageCursorStore, LineageFeedClient
from notifications.api.settings import get_ingress_settings
from notifications.config import get_notifications_settings
from notifications.inbox_actor import InboxActor
from notifications.watch_actor import WatchIndexActor
from service_kit.config import Settings
from service_kit.governed.dapr_auth import guard_actor_routes
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
    # One implementation, in service_kit: the annotator mounts DaprActor the same way and had the
    # same exposure, and two copies of an auth guard is how they drift.
    guard_actor_routes(app)
    return actor


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

        # The reconciler's egress, built ONCE. One `AsyncClient` serves both halves — lineage's feed
        # over the network and the sidecar's state API on localhost — because a pool is a property of
        # the process, and the cron route's alternative would be minting one per tick. Neither client
        # performs I/O at construction, so there is nothing here that can fail; what can fail is the
        # first tick, which answers 503 with the reason rather than starting a cursor-less walk.
        ingress = get_ingress_settings()
        app.state.http = httpx.AsyncClient()
        app.state.lineage_feed = LineageFeedClient(
            client=app.state.http,
            base_url=ingress.lineage_url,
            identity=ingress.service_identity,
            token=ingress.app_api_token,
            timeout_seconds=ingress.feed_timeout_seconds,
            page_limit=ingress.feed_page_limit,
        )
        app.state.lineage_cursor = LineageCursorStore(
            client=app.state.http,
            store_name=ingress.state_store,
            dapr_http_port=ingress.dapr_http_port,
        )

        actor_ext: DaprActor | None = getattr(app.state, "actor_ext", None)
        if actor_ext is not None:
            try:
                await actor_ext.register_actor(InboxActor)
                # The project-watch index — registered in the SAME try as the inbox, so a partial
                # actor plane is reported as unregistered rather than as half-working. A watch
                # endpoint that could write the subject's half and not the project's half would
                # produce exactly the split this plane refuses: a watch the settings page shows and
                # the fan-out never reads.
                await actor_ext.register_actor(WatchIndexActor)
                app.state.actors_registered = True
                log.info("notifications: InboxActor + WatchIndexActor registered")
            except Exception:
                app.state.actors_registered = False
                log.exception("notifications: actor registration failed — the inbox will 503")

        app.state.startup_complete = True
        app.state.shutting_down = False
        try:
            yield
        finally:
            app.state.shutting_down = True
            # Reverse construction order: the httpx pool was built AFTER the FGA client, so it closes
            # first. Suppressed for the same reason as the FGA close below — a shutdown path that
            # raises hides whatever came after it.
            http_client = getattr(app.state, "http", None)
            if http_client is not None:
                with suppress(Exception):
                    await http_client.aclose()
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
