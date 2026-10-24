"""The second ingress: lineage's durable run feed (`GET /events/projection`), walked from a persisted cursor.

**The bus is provably incomplete, and that is measured rather than defensive.** The ingest service
emits lineage over HTTP only and refuses the topic outright; so do Ray TRAIN and every external
OpenLineage producer posting to `/api/v1/lineage`. Those lanes reach lineage's durable feed
(`provenance$events`, a Lance table the catalog keeps, ADR 0158) and never `lineage.events.v1`. The one place both paths converge is this
feed — so a plane that subscribes and stops there is a plane that silently never notifies a whole
class of run.

**Three properties of lineage's feed shape this walk, none of them obvious from the endpoint name:**

1. `?since=<seq>` follows FORWARD, oldest first, and a row's seq is its stable Lance row id, assigned
   in commit order (`lineage.services.provenance`), so the mark is simply the last row handled:
   every row at or below it was offered, and no row below it can still appear (LH-472).
2. Each row's payload is lineage's TARGETING VIEW of the event (`events.py::targeting_view`): the run id,
   its `author` and `lance` facets, the state and time, and the output names — exactly what
   `lineage_events.py` reads, and nothing describing data. The feed row carries no `run_id` column, so
   the payload is the only place a notification id can come from.
3. The projection is NOT filtered per dataset; it is gated on the estate rung `can_read_event_feed`,
   held by the subject lineage maps this service account to through `event_reader`, which the chart's
   bootstrap hook grants ([[CTL-021]]). The governed `/events` shows a service only the tables its
   grants reach, so a run in any tenant warehouse it holds nothing on would never reach this lane. An
   ungranted subject fails every tick with a 403 rather than ticking cleanly over an empty view.
   Who is TOLD stays gated per person, at delivery (`can_be_notified`) and at render.

**A first-ever tick primes the cursor and notifies nobody.** With no stored cursor, "everything is
new" would mean replaying the retained feed into people's inboxes on the day the service is deployed
— the same failure `deliverPolicy: new` exists to prevent on the bus, arriving by the other door.

**THE RETRY IS THE SIDECAR'S, NOT THIS MODULE'S.** The feed is read through Dapr service invocation
(`IngressSettings.feed_base_url`), and the chart's `<release>-invocation-resiliency`, scoped to
`notifications`, retries that hop with `invokeRetry`: `408,429,500-599`, never a 4xx, because a 403's
answer will not change. A pass that still fails is re-walked by the next cron tick from the same mark,
because the cursor advances only on a pass that completes.

So this module carries NO retry of its own, which is the same rule the subscription handler has: one
layer owns redelivery, and a second would multiply it.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from notifications.api.fanout import ChannelPush, InboxOpener, WatcherLookup
from notifications.api.ingest import DAPR_RETRY, ingest_run_event
from notifications.api.metrics import Lane, record_feed_gap
from notifications.api.visibility import Visibility
from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.machine_identity import identity_bearer


log = logging.getLogger(__name__)

#: The state key the cursor lives under. One key for the service (not per subject), so it carries the
#: app's own name rather than an encoded subject (`notifications.subjects`).
CURSOR_KEY: Final = "notifications-lineage-cursor"


#: How many CONSECUTIVE passes may stop on the same row's retry before the walk steps over that row.
#:
#: The mark stops at a row that asked for a retry, which is right for a TRANSIENT failure: the row is
#: offered again next tick and nothing is lost. It is wrong for a PERMANENT one: a recipient whose actor
#: refuses forever would hold the mark there, and every notification above it would never be delivered
#: TO ANYONE while each tick logged `lineage_feed_reconciled`.
#:
#: Stepping over loses that one row, which is why it takes this many passes and an ERROR log to get
#: there: at the 30s cron period, ~5 minutes of the row failing every single time.
FEED_MAX_STALLS: Final = 10


class LineageCursorUnreadable(ServiceUnavailableError):
    """The stored cursor exists but cannot be read — never reported as absent.

    The distinction is the whole point. Absent means "prime and notify nobody", so an unreadable
    cursor read as absent would silently jump the high-water mark to the newest row and drop every
    notification in between — an outage turning into permanent, invisible data loss. Unreadable makes
    the tick fail instead, which an operator can see.
    """


class LineageFeedBudgetExceeded(ServiceUnavailableError):
    """One pass outran its own wall-clock budget.

    Distinct from a bare `TimeoutError` on purpose, and the distinction was earned: driving the live
    service found that `dapr.clients.http.client` calls a BLOCKING `DaprHealth.wait_for_sidecar()`
    when the first actor proxy is built, which raises `TimeoutError` after 60 s of its own. Catching
    the bare type reported a missing sidecar as "the pass exceeded its 25 s budget" — the wrong
    diagnosis, pointing at lineage for a fault in the delivery plane.
    """


class LineageCursor(BaseModel):
    """The reconciler's mark: the last feed row this lane handled. Every row at or below it was offered."""

    #: `ignore`, not `forbid`: this record is SERVICE-INTERNAL and single-writer, so an unknown field
    #: carries no other subject's data, and a forbid would turn version skew between builds into
    #: `LineageCursorUnreadable`, stopping the reconciler. Unreadable stays fatal for a CORRUPT cursor.
    model_config = ConfigDict(frozen=True, extra="ignore")

    seq: int = Field(ge=0)
    updated_at: datetime
    #: Consecutive passes that stopped on a retry at the row after `seq`. Reset by any pass that moves on.
    stalls: int = Field(default=0, ge=0)


class FeedRecord(BaseModel):
    """One row of lineage's run feed: its `seq` and the event's targeting view, which this plane parses
    with the same projection the bus lane uses, so that the two lanes cannot disagree about what an event
    says."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    seq: int
    event: dict[str, Any] = Field(default_factory=dict)


class FeedPage(BaseModel):
    """One page of the feed: oldest first after `since`, and `next_cursor` is the `since` to pass next.

    The priming read (no `since`) is the one newest-first page this lane asks for.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    events: list[FeedRecord] = Field(default_factory=list)
    next_cursor: int | None = None
    #: The oldest seq lineage still RETAINS. `None` = an empty feed, or a lineage predating the field
    #: — and both must read as "cannot tell", never as a gap, or the detector cries wolf on every tick
    #: against a healthy older deployment.
    oldest_seq: int | None = None


class ReconcileResult(BaseModel):
    """What one tick did. Counts and a cursor — never a subject."""

    model_config = ConfigDict(frozen=True)

    scanned: int = 0
    retried: int = 0
    cursor: int | None = None
    #: The first-ever tick: the cursor was set to the newest row and nothing was delivered.
    primed: bool = False
    #: The pass used its page budget before catching up. Not a loss: the next tick continues from the mark.
    behind: bool = False
    #: Lineage's retention deleted rows this lane had not read. Unrecoverable, so the mark moves past
    #: them and the loss is reported once, never per tick.
    gapped: bool = False
    #: A pass was already in flight, so this tick did nothing. Reported rather than silent: a lane
    #: that is permanently skipping is a lane whose budget no longer fits its schedule.
    skipped: bool = False


class LineageFeedClient:
    """Reads pages of lineage's durable event feed through its SERVICE door.

    The client is INJECTED rather than built here: one connection pool belongs to the process, and a
    poller that minted its own per tick would open and discard one every cron interval. The lifespan
    owns it, as it owns every other client in the estate.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        token_file: str,
        timeout_seconds: float,
        page_limit: int,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._token_file = token_file
        self._timeout = timeout_seconds
        self._page_limit = page_limit

    def _headers(self) -> dict[str, str]:
        """This pod's `rask-lineage` service-account token as a bearer, read from the file NOW.

        Per request, never once per process: the kubelet rewrites the projected token at ~515 s of its
        600 s life (measured, LH-220 probe d). Lineage derives the caller from the verified token, so
        nothing here names a subject. Through Dapr service invocation the sidecar forwards
        `Authorization` unchanged (LH-220 probe e). An unreadable file raises
        `IdentityTokenUnavailableError` (503) rather than sending an anonymous walk, so the failure
        names the missing token instead of reading as lineage's 401.
        """
        return identity_bearer(self._token_file)

    async def page(self, *, since: int | None) -> FeedPage:
        """The rows after `since`, oldest first; with `since` absent, the newest page (priming only)."""
        params: dict[str, str] = {"limit": str(self._page_limit)}
        if since is not None:
            params["since"] = str(since)
        response = await self._client.get(f"{self._base}/events/projection", params=params, headers=self._headers(), timeout=self._timeout)
        response.raise_for_status()
        return FeedPage.model_validate(response.json())


class LineageCursorStore:
    """The reconciler's cursor, in the sidecar's plain state API.

    The plain state API rather than the actor API on purpose: the cursor belongs to the SERVICE, not
    to any subject, and putting it on an actor would invent a subject to own it. Every failure to
    reach the store fails CLOSED — see :class:`LineageCursorUnreadable`.
    """

    def __init__(self, *, client: httpx.AsyncClient, store_name: str, dapr_http_port: int = 3500, timeout_seconds: float = 5.0) -> None:
        self._client = client
        self._base = f"http://localhost:{dapr_http_port}/v1.0/state/{store_name}"
        self._timeout = timeout_seconds

    async def get(self) -> LineageCursor | None:
        """The stored cursor, or `None` when this service has never run here.

        Dapr answers a missing key with `204 No Content`, which is the ordinary first-tick case. Every
        other failure — transport, a non-2xx, an unparseable record — raises.
        """
        try:
            response = await self._client.get(f"{self._base}/{CURSOR_KEY}", timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LineageCursorUnreadable("the lineage feed cursor could not be read") from exc
        if response.status_code == httpx.codes.NO_CONTENT or not response.content:
            return None
        try:
            return LineageCursor.model_validate_json(response.content)
        except ValidationError as exc:
            log.exception("lineage_cursor_unreadable")
            raise LineageCursorUnreadable("the stored lineage feed cursor no longer fits its schema") from exc

    async def set(self, seq: int, *, stalls: int = 0) -> None:
        """Move the mark. Last write wins — one writer, no contender to race."""
        cursor = LineageCursor(seq=seq, updated_at=datetime.now(UTC), stalls=stalls)
        try:
            response = await self._client.post(
                self._base,
                json=[{"key": CURSOR_KEY, "value": cursor.model_dump(mode="json")}],
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LineageCursorUnreadable("the lineage feed cursor could not be written") from exc


async def reconcile(
    *,
    client: LineageFeedClient,
    store: LineageCursorStore,
    visibility: Visibility,
    open_inbox: InboxOpener,
    watchers: WatcherLookup | None = None,
    push: ChannelPush | None = None,
    max_pages: int,
    budget_seconds: float,
) -> ReconcileResult:
    """One catch-up pass FORWARD from the mark. Bounded in pages AND in wall-clock.

    **The mark never passes a row this lane did not handle.** It advances row by row as each delivery
    succeeds and stops at the first row that asks for a RETRY, so the next tick offers that row first.
    Everything the pass handled before a retry, a budget expiry or the page bound is kept: a forward mark
    can record partial progress, so a backlog deeper than one tick drains across ticks.

    Pages are handled as they arrive rather than buffered, so the walk's length bounds time and
    requests but never memory.

    The hard budget is `asyncio.timeout`, covering the cursor READ and the PRIMING poll as well as the
    walk. Only the cursor WRITE is outside it, deliberately: progress made inside the budget must not be
    lost to a timer firing during the write that records it.
    """
    stored: LineageCursor | None = None
    progress = _Progress()
    budget = asyncio.timeout(budget_seconds)
    try:
        async with budget:
            stored = await store.get()
            if stored is None:
                return await _prime(client, store)
            progress.mark = stored.seq
            await _walk(
                client,
                stored,
                progress,
                visibility=visibility,
                open_inbox=open_inbox,
                watchers=watchers,
                push=push,
                max_pages=max_pages,
            )
    except TimeoutError as exc:
        if not budget.expired():
            # Somebody else's TimeoutError — the Dapr SDK's blocking sidecar health check is the one
            # that actually occurs. It is not a statement about the feed, so it propagates as itself.
            raise
        if stored is not None and progress.mark > stored.seq:
            # A gap found before the timer fired is reported now: the saved mark is past it, so no later
            # tick will see it again.
            _report_gap(stored, progress)
            await store.set(progress.mark)
            log.warning("lineage_feed_walk_cut_short", extra={"cursor": progress.mark, "scanned": progress.scanned})
        raise LineageFeedBudgetExceeded(f"the lineage feed reconcile pass exceeded its {budget_seconds}s budget") from exc
    return await _settle(store, stored, progress)


class _Progress:
    """What a pass has done so far, kept outside the walk so a budget expiry can still save it."""

    def __init__(self) -> None:
        self.mark = 0
        self.scanned = 0
        #: The seq of the row that asked for a retry, which ended the pass.
        self.retry_at: int | None = None
        self.behind = True
        self.gapped = False


async def _walk(
    client: LineageFeedClient,
    stored: LineageCursor,
    progress: _Progress,
    *,
    visibility: Visibility,
    open_inbox: InboxOpener,
    watchers: WatcherLookup | None,
    push: ChannelPush | None,
    max_pages: int,
) -> None:
    """Offer every row after the mark, oldest first, until caught up, a retry, or `max_pages`."""
    for _ in range(max_pages):
        page = await client.page(since=progress.mark)
        # A FEED REBUILT BENEATH THE MARK. Lineage answers a `since` past its newest row with that row's seq, which
        # no cursor of this feed can exceed, so a lower `next_cursor` means the mark was taken on another feed
        # (lineage's store moved, or was restored). Re-read this feed from its oldest row: a row offered twice is
        # absorbed by the inbox's idempotent ids, and a row never offered is the loss this lane exists to prevent.
        if page.next_cursor is not None and page.next_cursor < progress.mark:
            log.warning("lineage_feed_cursor_ahead_of_feed", extra={"cursor": progress.mark, "head": page.next_cursor})
            progress.mark = page.oldest_seq - 1 if page.oldest_seq is not None else 0
            continue
        # DID THE FEED DELETE ROWS WE HAD NOT READ? Lineage serves rows at or above `oldest_seq`; if that
        # is past the row after our mark, the rows between were pruned before this lane reached them.
        # The loss is unrecoverable, so the mark moves past it and it is reported this once.
        if page.oldest_seq is not None and page.oldest_seq > progress.mark + 1:
            progress.gapped = True
            progress.mark = page.oldest_seq - 1
        # Lineage never returns a row at or below `since`; holding it to that here is what keeps a feed
        # that ignores `since` from re-offering rows forever or moving the mark backward.
        fresh = [record for record in page.events if record.seq > progress.mark]
        for record in fresh:
            progress.scanned += 1
            # The first row of a pass that follows a stalled one is the row that asked for the retry.
            offered_again = bool(stored.stalls) and progress.scanned == 1
            # `watchers` and `push` are forwarded: this lane is the only one for runs the bus never
            # carries (ingest, external OpenLineage producers), so dropping them here would leave every
            # one of those runs with no watcher resolved and no email or Slack sent.
            status = await ingest_run_event(
                record.event,
                lane=Lane.FEED,
                visibility=visibility,
                open_inbox=open_inbox,
                watchers=watchers,
                push=push,
                event_seq=record.seq,
                # A row offered again after it asked for a retry: its permanent fault, if it has one,
                # was already reported, and must not be reported again on every tick.
                first_sight=not offered_again,
            )
            if status == DAPR_RETRY:
                progress.retry_at = record.seq
                progress.behind = False
                return
            progress.mark = record.seq
        if page.next_cursor is not None:
            progress.mark = max(progress.mark, page.next_cursor)
        if not fresh:
            progress.behind = False
            return


async def _settle(store: LineageCursorStore, stored: LineageCursor, progress: _Progress) -> ReconcileResult:
    """Write the mark the walk earned and report what it cost to get there."""
    _report_gap(stored, progress)
    if progress.retry_at is None:
        await store.set(progress.mark)
        return ReconcileResult(scanned=progress.scanned, cursor=progress.mark, behind=progress.behind, gapped=progress.gapped)
    # Stopped at a row asking for a retry. The same row as last pass counts toward the stall bound; a
    # pass that moved the mark first starts the count again.
    stalls = stored.stalls + 1 if progress.mark == stored.seq else 1
    if stalls < FEED_MAX_STALLS:
        await store.set(progress.mark, stalls=stalls)
        return ReconcileResult(scanned=progress.scanned, retried=1, cursor=progress.mark, gapped=progress.gapped)
    log.error("lineage_feed_poison_skipped", extra={"cursor": progress.mark, "skipped": progress.retry_at, "stalls": stalls})
    await store.set(progress.retry_at)
    return ReconcileResult(scanned=progress.scanned, retried=1, cursor=progress.retry_at, gapped=progress.gapped)


def _report_gap(stored: LineageCursor, progress: _Progress) -> None:
    """Retention deleted rows this lane had not read. ERROR because the loss is unrecoverable, and every
    tick that hid it would make the estate look healthier than it is.

    Every seq below lineage's `oldest_seq` was a row: a seq is one more than a stable Lance row id, and Lance advances
    its `next_row_id` by exactly the rows a commit adds, reassigning them when a conflict rebases the
    commit (`lance_docs/file_format.md`, "Row ID Assignment"), so the feed issues no seq it never filled.
    """
    if progress.gapped:
        log.error("lineage_feed_pruned_below_cursor", extra={"cursor": stored.seq, "scanned": progress.scanned})
        record_feed_gap()


async def _prime(client: LineageFeedClient, store: LineageCursorStore) -> ReconcileResult:
    """First tick here: adopt the newest row as the mark and tell nobody about the history behind it."""
    page = await client.page(since=None)
    if not page.events:
        return ReconcileResult(primed=True)
    # `max` rather than a position: the order of the priming page is the server's to choose.
    newest = max(record.seq for record in page.events)
    await store.set(newest)
    log.info("lineage_feed_cursor_primed", extra={"cursor": newest})
    return ReconcileResult(cursor=newest, primed=True)
