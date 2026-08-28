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
from service_kit.draining import arm_drain_on_sigterm
from service_kit.governed.actor_state_store import probe_actor_state_store
from service_kit.governed.actor_warmup import warm_actor_proxy_factory
from service_kit.governed.auth_lifespan import attach_auth
from service_kit.governed.dapr_auth import guard_actor_routes
from service_kit.governed.fga import dispose as fga_dispose
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
        # logged and non-fatal — the estate default — so the dependency finds no verifier/client on
        # app.state and answers 503, where falling back to a permissive checker would turn a broken
        # authorization layer into an open one, on a plane whose whole point is that a badge counts
        # YOUR work.
        await attach_auth(app, notifications_settings, service="notifications")

        # The reconciler's egress, built ONCE. One `AsyncClient` serves both halves — lineage's feed
        # over the network and the sidecar's state API on localhost — because a pool is a property of
        # the process, and the cron route's alternative would be minting one per tick. Neither client
        # performs I/O at construction, so there is nothing here that can fail; what can fail is the
        # first tick, which answers 503 with the reason rather than starting a cursor-less walk.
        ingress = get_ingress_settings()
        app.state.http = httpx.AsyncClient()
        app.state.lineage_feed = LineageFeedClient(
            client=app.state.http,
            base_url=ingress.feed_base_url,
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
                # Pay the SDK's BLOCKING sidecar handshake HERE rather than on the first request.
                # MEASURED against a real process: without it, one cron tick against an unreachable
                # sidecar pinned the event loop for 60 s and the pass's own `asyncio.timeout` never
                # fired — a wall-clock budget cannot bound work that blocks the scheduler it runs on.
                # Advisory: the routes already gate on `actors_registered`, so a cold factory is a
                # slow first call, never a wrong answer.
                await warm_actor_proxy_factory()
                # Everything above is PROCESS-LOCAL and cannot fail for a missing actor state
                # store: registration never touches the sidecar, so this reports success while
                # every actor call still refuses. Ask the sidecar what it can actually see.
                await probe_actor_state_store(capability="the inbox cannot hold anything and the bell will never fill")
            except Exception:
                app.state.actors_registered = False
                log.exception("notifications: actor registration failed — the inbox will 503")

        app.state.startup_complete = True
        app.state.shutting_down = False
        try:
            # ARMED AT SIGTERM, not at lifespan shutdown. The flag below flips in the `finally`,
            # which uvicorn only reaches AFTER it has stopped accepting connections and drained —
            # so the admission guards that read it refused nothing, ever. Kubernetes sends SIGTERM
            # at the START of termination, and that window is exactly when the sidecar is still
            # delivering. Owner ruling 2026-08-25.
            _disarm_drain = arm_drain_on_sigterm(app)
            yield
        finally:
            _disarm_drain()
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
            # This block was the estate's only correct one, and is now the shared
            # `service_kit.governed.fga.dispose` its four siblings adopted — one disposer beside the
            # factory rather than five copies, four of which were missing.
            await fga_dispose(app)

    return lifespan
