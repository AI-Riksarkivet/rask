"""The reconciler's TICK — the cron binding that turns the `/events` walk into a running lane.

`notifications.api.reconciler` knows how to walk lineage's durable feed from a cursor; nothing called
it. This module is that caller, and it is a ROUTE rather than a background task on purpose: a Dapr
input binding is delivered as `POST /<component name>` at the root, so the schedule lives in the chart
as component config and this service keeps no scheduler thread, no `asyncio.create_task`, and no
second thing to drain on shutdown. Same shape as `compute.pruner` and `maintenance`'s two crons.

**The route path IS the binding name.** They are one string (`IngressSettings.binding_name`), because
a component whose name and route disagree is a cron that fires into a 404 on every tick while both
halves look right in isolation.

**`require_dapr_token` is load-bearing here, not ceremony.** An unauthenticated trigger would let any
client that can reach the port drive an unbounded number of catch-up walks against lineage — and,
because the feed is governed by THIS service's identity, drive them with credentials the caller does
not hold. The dependency also refuses invocations that arrived through a public front door, which is
the measured gateway-bypass class.

**Failures answer, they do not crash.** A tick that cannot read its cursor, cannot reach lineage, or
outruns its budget raises a domain error that `make_service_app`'s handlers render as problem+json.
The next tick simply finds the same work to do — the cursor only advances on a pass that fully
succeeded, so nothing is lost by failing loudly and nothing is duplicated by trying again.
"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from notifications.api.metrics import PassOutcome, record_feed_gap, record_pass
from notifications.api.reconciler import LineageCursorStore, LineageFeedClient, ReconcileResult, reconcile
from notifications.api.security import VisibilityDep
from notifications.api.settings import get_ingress_settings
from notifications.dependencies import ActorPlaneDep
from notifications.proxies import channel_push, inbox_for, watchers_of
from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)

router = APIRouter()

#: Single-flight, the shape both cron precedents carry (`maintenance/api/routes.py` has one per cron).
#: Dapr's cron binding has NO overlap protection: if a pass outruns its period the next tick is
#: delivered anyway, and two concurrent passes read the same un-advanced cursor and re-walk the same
#: rows — doubling the FGA and actor load exactly when lineage or the sidecar is already the slow
#: thing. Skipping is the right answer rather than queueing: the work is not lost, it is what the pass
#: already in flight is doing.
_reconcile_lock = asyncio.Lock()


def get_feed_client(request: Request) -> LineageFeedClient:
    """The lifespan-built feed client, or an honest 503.

    Absent means the lifespan did not run or could not build it — never "build one now". A per-tick
    client would open and discard a connection pool every cron interval, which is exactly what
    `LineageFeedClient`'s own docstring refuses.
    """
    client = getattr(request.app.state, "lineage_feed", None)
    if client is None:
        raise ServiceUnavailableError("the lineage feed client is unavailable — the reconciler cannot run")
    return client


FeedClientDep = Annotated[LineageFeedClient, Depends(get_feed_client)]


def get_cursor_store(request: Request) -> LineageCursorStore:
    """The lifespan-built cursor store, or an honest 503 — never a silently cursor-less walk.

    Fail-closed for the reason `LineageCursorUnreadable` exists: a missing store read as "no cursor"
    would prime the high-water mark to the newest row and drop every notification behind it.
    """
    store = getattr(request.app.state, "lineage_cursor", None)
    if store is None:
        raise ServiceUnavailableError("the lineage cursor store is unavailable — the reconciler cannot run")
    return store


CursorStoreDep = Annotated[LineageCursorStore, Depends(get_cursor_store)]


async def on_reconcile_cron(feed: FeedClientDep, cursor: CursorStoreDep, visibility: VisibilityDep, _: ActorPlaneDep) -> ReconcileResult:
    """One catch-up pass over lineage's durable feed, on the chart's schedule.

    Gated on the actor plane for the same reason the inbox routes are, plus one this route learned the
    hard way. Without a registered actor plane there is nowhere to deliver, so a walk would burn the
    feed and lineage's governed read path to produce pointers nothing can store. Worse, MEASURED
    against a real process: the Dapr SDK builds its proxy factory lazily and that constructor calls a
    BLOCKING `DaprHealth.wait_for_sidecar()`, so the first delivery on a sidecar-less deployment —
    which is what `make dev-micro` runs — pins the event loop for 60 s and the pass budget cannot fire,
    because the loop it would fire on is the one that is blocked. Refusing early is what keeps a
    sidecar-less dev fleet honest rather than hung.

    The settings are read at call time rather than captured at import so a test can rebuild them; the
    `lru_cache` makes that free.
    """
    settings = get_ingress_settings()
    if _reconcile_lock.locked():
        log.warning("lineage_feed_tick_skipped_overlap")
        record_pass(PassOutcome.SKIPPED)
        return ReconcileResult(skipped=True)
    async with _reconcile_lock:
        # `reconcile()` raises `LineageFeedBudgetExceeded` (a domain error → 503) when its own budget
        # fires, and lets every other failure keep its own type and traceback. Nothing is caught here:
        # routes raise domain exceptions and the app's handlers shape the response.
        result = await reconcile(
            client=feed,
            store=cursor,
            visibility=visibility,
            open_inbox=inbox_for,
            watchers=watchers_of,
            push=channel_push(),
            max_pages=settings.feed_max_pages,
            budget_seconds=settings.reconcile_budget_seconds,
        )
    log.info(
        "lineage_feed_reconciled",
        extra={
            "scanned": result.scanned,
            "retried": result.retried,
            "cursor": result.cursor,
            "primed": result.primed,
            "truncated": result.truncated,
            "gapped": result.gapped,
        },
    )
    record_pass(PassOutcome.COMPLETED)
    if not result.gapped:
        # The reconciler already added 1 when gapped; this creates the series on tick 1 so an alert over
        # it can evaluate before the first loss rather than reading "no data" forever.
        record_feed_gap(0)
    return result


async def ack_binding() -> Response:
    """Dapr's OPTIONS binding-discovery pre-flight.

    Without it the app answers 405 and Dapr logs the binding as not consumed — the component exists,
    the schedule ticks, and nothing is ever delivered.
    """
    return Response(status_code=200)


_binding = get_ingress_settings().binding_name
router.add_api_route(
    f"/{_binding}",
    on_reconcile_cron,
    methods=["POST"],
    tags=["notifications"],
    dependencies=[Depends(require_dapr_token)],
)
router.add_api_route(f"/{_binding}", ack_binding, methods=["OPTIONS"], include_in_schema=False)
