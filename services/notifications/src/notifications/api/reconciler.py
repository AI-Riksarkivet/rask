"""The second ingress: lineage's durable `GET /events` feed, walked from a persisted cursor.

**The bus is provably incomplete, and that is measured rather than defensive.** The ingest service
emits lineage over HTTP only and refuses the topic outright; so do Ray TRAIN and every external
OpenLineage producer posting to `/api/v1/lineage`. Those lanes reach lineage's durable feed
(`public.lineage_events`) and never `lineage.events.v1`. The one place both paths converge is this
feed — so a plane that subscribes and stops there is a plane that silently never notifies a whole
class of run.

**Three properties of lineage's feed shape this walk, none of them obvious from the endpoint name:**

1. `?after=<seq>` pages OLDER, not newer: the SQL is `WHERE seq < %s ORDER BY seq DESC`. So catching
   up is a walk DOWN from the newest row until the stored high-water mark comes into view — there is
   no "give me everything since" call to make.
2. `summary=true` cannot be used here. It drops the `event` payload at the SQL layer, and the feed's
   row carries **no `run_id` column in either mode** — the id lives only inside that payload. A
   summary row therefore cannot produce a notification id at all, so the reconciler asks for the full
   record and pays for it.
3. The feed is GOVERNED. It is filtered by the caller's own visibility, so this service's
   `x-lance-service-identity` sees exactly the rows its service principal is granted — and a
   deployment that forgets those grants gets a reconciler that runs cleanly and reconciles nothing.

**A first-ever tick primes the cursor and notifies nobody.** With no stored cursor, "everything is
new" would mean replaying the retained feed into people's inboxes on the day the service is deployed
— the same failure `deliverPolicy: new` exists to prevent on the bus, arriving by the other door.

**THE RETRY IS THE SIDECAR'S, NOT THIS MODULE'S.** It used to be tenacity's, on the argument that this
is the service's own egress and "no sidecar policy covers it" — true only because the feed was read
over a DIRECT httpx call, and nothing recorded why it had to be. Read through Dapr service invocation
(`IngressSettings.feed_base_url`), the estate's existing `invokeRetry` policy covers it, and that
policy already encodes the rule the hand-written `_is_transient` did: `408,429,500-599`, never a 4xx,
for the reason its own comment gives — "Retrying a 403 is also pointless on its own terms: the answer
will not change." `lineage` is already one of that policy's targets.

So this module now carries NO retry of its own, which is the same rule the subscription handler always
had: one layer owns redelivery, and a second would multiply it.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from notifications.api.fanout import ChannelPush, InboxOpener, WatcherLookup
from notifications.api.ingest import DAPR_RETRY, ingest_run_event
from notifications.api.metrics import Lane, record_feed_gap
from notifications.api.visibility import Visibility
from service_kit.exceptions import ServiceUnavailableError


log = logging.getLogger(__name__)

#: The state key the cursor lives under. One key for the service (not per subject), so it carries the
#: app's own name rather than going through the user-state key derivation.
CURSOR_KEY: Final = "notifications-lineage-cursor"


#: How far below the mark the walk re-offers rows, in feed seqs.
#:
#: `seq` is a `bigserial` allocated at INSERT while lineage commits autocommit per event, so commit
#: order is NOT seq order: writer A can hold 1000 while writer B takes 1001 and commits first. A tick
#: reading in that window sees 1001 and not 1000, and a mark set to 1001 puts row 1000 permanently
#: below the filter — its author and watchers never told, with no gap log, because the page was not
#: truncated. Re-offering a bounded window closes it.
#:
#: Free by construction, and only because two other properties hold: delivery is idempotent on the
#: notification's natural key, so a re-offer is a counted duplicate rather than a second
#: notification; and `dismiss` MARKS a pointer rather than deleting it, so a re-offered dismissal
#: stays dismissed.
#:
#: A CONSTANT, not a setting: it describes how lineage writes, not how anyone deploys, so there is no
#: correct per-cluster value to tune.
FEED_OVERLAP: Final = 64

#: How many CONSECUTIVE passes may end in a retry before the walk steps over the rows asking for it.
#:
#: The mark advances only on a clean pass, which is right for a TRANSIENT failure — the row is
#: re-offered next tick and nothing is lost. It is wrong for a PERMANENT one: a recipient whose actor
#: refuses forever makes every pass end `retried > 0`, so the mark never moves and every notification
#: above it is never delivered TO ANYONE. The lane stalls while each tick logs `lineage_feed_reconciled`
#: and reports progress it did not make.
#:
#: Stepping over loses those rows, which is why it takes this many passes and an ERROR log to get there:
#: at the 30s cron period, ~5 minutes of a row failing every single time. Retrying forever loses every
#: row above it instead, and silently — the strictly worse trade.
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
    """The reconciler's high-water mark: every feed row at or below `seq` has been handled.

    `resume_from` and `pending_high` exist because the walk runs DOWNWARD and the mark is a LOW-water
    one, which makes partial progress otherwise unrepresentable. A pass that handles the newest 3 000
    rows and is then cut off cannot raise `seq` — the rows between `seq` and where it stopped are
    still unhandled — so without somewhere to record "I got this far coming down", the next tick
    starts at the newest row again, dies at the same depth, and the backlog is never drained. That is
    a permanent stall rather than a slow catch-up, and it is exactly the state an outage produces.

    So an interrupted walk parks two facts: `resume_from`, the feed cursor the next tick continues
    from instead of starting at the top, and `pending_high`, the newest row that walk had reached —
    which becomes the new `seq` only when the walk finally meets the old one. Both are `None` in the
    settled state, which is every cursor S1 ever wrote (hence the defaults, and hence a stored record
    from before this field existed still validates).
    """

    #: `ignore`, not `forbid`, and the difference is a rollback away from an outage. This record is
    #: SERVICE-INTERNAL and single-writer, so an unknown field carries no other subject's data —
    #: there is nothing here for a forbid to contain, and the inbox row's cross-subject argument for
    #: keeping one does not apply. What a forbid DID do was turn routine version skew into
    #: `LineageCursorUnreadable`: fields added to this model met an older build and refused the whole
    #: cursor, stopping the reconciler — the ingress that exists because the bus alone is provably
    #: incomplete. Unreadable stays fatal for a CORRUPT cursor (reading one as absent would jump the
    #: mark to the newest row and drop every notification in between); a field a newer build added is
    #: not corruption.
    model_config = ConfigDict(frozen=True, extra="ignore")

    seq: int = Field(ge=0)
    updated_at: datetime
    #: Where an interrupted downward walk stopped. `None` = the last pass completed.
    resume_from: int | None = Field(default=None, ge=0)
    #: The newest row the interrupted walk had reached; becomes `seq` when it completes.
    pending_high: int | None = Field(default=None, ge=0)
    #: Consecutive passes that ended in a retry without moving the mark. Reset by any clean pass.
    stalls: int = Field(default=0, ge=0)
    #: The seq `FEED_OVERLAP` may never reach below — the rows this deployment deliberately SKIPPED.
    #:
    #: Without it the overlap is not merely imprecise, it is wrong in the one direction that matters.
    #: A first-ever tick primes the mark to the newest row and notifies nobody, on purpose: replaying
    #: the retained feed into everyone's inbox on deployment day is the failure `deliverPolicy: new`
    #: prevents on the bus. An unclamped overlap reaches BELOW that primed mark on the very next tick
    #: and delivers exactly the backlog priming had just promised to skip. (Built, observed breaking
    #: four tests for precisely this reason, and reverted before it shipped.)
    #:
    #: `None` on a cursor written before this field existed. That case adopts the CURRENT mark as the
    #: floor rather than guessing what an older deployment skipped — one pass with no overlap, and
    #: every pass after it protected.
    floor: int | None = Field(default=None, ge=0)


class FeedRecord(BaseModel):
    """One row of lineage's durable feed. Only `seq` and the raw event are read; the summary columns
    beside them are lineage's own projection and this plane derives its own from the payload, so that
    the bus lane and this one cannot disagree about what an event says."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    seq: int
    event: dict[str, Any] = Field(default_factory=dict)


class FeedPage(BaseModel):
    """One page of the feed, newest first. `next_cursor` is `None` when the feed is exhausted."""

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
    #: The walk ran out of pages before reaching the cursor — rows were skipped. See `feed_max_pages`.
    truncated: bool = False
    #: Lineage's feed had already PRUNED rows this lane had not read. Distinct from `truncated`, which
    #: is the walk running out of pages: this one exits through the success door — the feed floor comes
    #: into view, the walk breaks clean, and the loss is invisible without lineage's `oldest_seq`.
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
        identity: str,
        token: SecretStr | None,
        timeout_seconds: float,
        page_limit: int,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._identity = identity
        self._token = token
        self._timeout = timeout_seconds
        self._page_limit = page_limit

    def _headers(self) -> dict[str, str]:
        """The service-door pair, or nothing at all.

        BOTH headers or NEITHER, because lineage's door opens on the pair: a request carrying only
        `dapr-api-token` falls through to OIDC by design (the sidecar stamps that token on every
        request it delivers, so gating on it alone would divert proxied humans into the service door),
        and one carrying only the identity is an unauthenticated claim. Without an `APP_API_TOKEN`
        there is no service door in this deployment, so sending half of it would only produce a 401
        that reads like a credential problem instead of a configuration one.
        """
        if self._token is None:
            return {}
        return {"dapr-api-token": self._token.get_secret_value(), "x-lance-service-identity": self._identity}

    async def page(self, *, after: int | None) -> FeedPage:
        """One page of the feed, newest first; `after` walks OLDER (`seq < after`).

        `summary=false` is explicit at the wire rather than left to the server's default, because it
        is the load-bearing half of this call: the summary projection has no run id anywhere in it.
        """
        params: dict[str, str] = {"limit": str(self._page_limit), "summary": "false"}
        if after is not None:
            params["after"] = str(after)
        response = await self._client.get(f"{self._base}/events", params=params, headers=self._headers(), timeout=self._timeout)
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

    async def set(self, seq: int, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None, stalls: int = 0) -> None:
        """Move the mark. Last write wins — one writer, no contender to race.

        Called with `resume_from`/`pending_high` to PARK an interrupted walk (the mark itself does not
        move, because the rows below where it stopped are still unhandled), and without them to settle
        a completed one — which also clears any parked state, so a walk that finishes leaves nothing
        behind for the next tick to resume from.
        """
        cursor = LineageCursor(seq=seq, updated_at=datetime.now(UTC), resume_from=resume_from, pending_high=pending_high, floor=floor, stalls=stalls)
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
    """One catch-up pass over the feed. Bounded in pages AND in wall-clock.

    **The cursor advances only when the whole pass succeeded.** If any row asked for a RETRY, the mark
    stays put and the next tick re-offers everything above it — including the rows that already landed,
    which is free: delivery is idempotent on the notification's natural key, so a re-offer is a
    counted duplicate rather than a second notification. Advancing past a failure would be the one
    thing this lane cannot undo.

    Pages are handled as they arrive rather than buffered, so the walk's length bounds time and
    requests but never memory: a catch-up across the feed's entire retention holds one page at a time.

    The hard budget is `asyncio.timeout`, so a lineage that answers slowly fails this tick instead of
    letting the next cron delivery stack on top of it. It covers the cursor READ and the PRIMING poll
    as well as the walk — the priming poll is a full sidecar-retried request, so a first-ever tick
    against a sick lineage is the longest thing this function can do and the one an unbounded version
    would let stack. Only the cursor WRITE is outside it, deliberately: a walk that finished inside the
    budget must not lose the progress it made to a timer firing during the write that records it.
    """
    scanned = 0
    retried = 0
    truncated = True
    gapped = False
    stored = None
    after: int | None = None
    high = 0
    budget = asyncio.timeout(budget_seconds)
    try:
        async with budget:
            stored = await store.get()
            if stored is None:
                return await _prime(client, store)
            # Continue an interrupted walk from where it stopped rather than from the newest row.
            # `pending_high` carries that walk's ceiling, so the mark it eventually settles on is the
            # top of the WHOLE multi-tick walk, not the top of whichever tick happened to finish it.
            after = stored.resume_from
            high = stored.pending_high if stored.pending_high is not None else stored.seq
            # THE MARK TRAILS BY `FEED_OVERLAP`, CLAMPED TO THE FLOOR. See both constants: the trail
            # closes lineage's commit-order window (a row still mid-INSERT when a tick read is
            # otherwise below the mark forever), and the clamp keeps it from reaching into the backlog
            # a first-ever prime deliberately skipped.
            #
            # `floor is None` is a cursor written before the field existed: adopt the current mark, so
            # this pass behaves exactly as it always did and the floor is recorded on the way out.
            settled_floor = stored.floor if stored.floor is not None else stored.seq
            walk_floor = max(stored.seq - FEED_OVERLAP, settled_floor)
            for _ in range(max_pages):
                page = await client.page(after=after)
                # DID THE FEED DELETE ROWS WE HAD NOT READ? The walk wants everything above
                # `walk_floor`; lineage can only serve rows at or above its own floor. If its floor has
                # climbed past ours, the rows in between were pruned before this lane reached them.
                #
                # Checked per PAGE rather than once, because the prune runs inline on every ingest and
                # can therefore move underneath a walk that is already in progress.
                #
                # `None` means "cannot tell" — an empty feed, or a lineage older than the field — and
                # must never read as a gap: a detector that fires every tick against a healthy older
                # deployment is one nobody keeps listening to.
                if page.oldest_seq is not None and page.oldest_seq > walk_floor + 1:
                    gapped = True
                fresh = [record for record in page.events if record.seq > walk_floor]
                for record in fresh:
                    scanned += 1
                    high = max(high, record.seq)
                    # `watchers` and `push` are forwarded, and both were once dropped right here while
                    # the signature above accepted them and the cron route dutifully passed them. The
                    # cost fell entirely on this lane, which is the lane that matters most for it: the
                    # bus never sees ingest, Ray TRAIN or any external OpenLineage producer, so for
                    # every one of those runs project watchers were never resolved and no email or
                    # Slack was ever sent — while the pass logged `lineage_feed_reconciled` and the
                    # author's inbox row landed, making it look like the plane worked.
                    status = await ingest_run_event(
                        record.event,
                        lane=Lane.FEED,
                        visibility=visibility,
                        open_inbox=open_inbox,
                        watchers=watchers,
                        push=push,
                        event_seq=record.seq,
                    )
                    if status == DAPR_RETRY:
                        retried += 1
                if len(fresh) < len(page.events) or page.next_cursor is None:
                    # The cursor came into view, or the feed floor did — either way the walk is complete.
                    truncated = False
                    break
                after = page.next_cursor
    except TimeoutError as exc:
        if not budget.expired():
            # Somebody else's TimeoutError — the Dapr SDK's blocking sidecar health check is the one
            # that actually occurs. It is not a statement about the feed, so it must not be parked as
            # one and must not be renamed into a budget overrun; it propagates with its own traceback.
            raise
        # Park the descent so the next tick resumes instead of restarting. Without this the walk is
        # all-or-nothing: a backlog deeper than one budget re-walks the same prefix forever, times out
        # at the same depth, and the rows below it are never delivered — a permanent stall wearing the
        # costume of a transient one.
        #
        # Only when nothing asked for a RETRY: parking past a row that failed would skip it, and the
        # whole reason the mark is held back on retry is that this lane must not step over a failure.
        # `after is not None` means at least one full page was handled, so there is progress to keep;
        # a budget too small for a SINGLE page parks nothing and stalls honestly, which is a
        # `feed_page_limit`/`reconcile_budget_seconds` misconfiguration and reads as one.
        if stored is not None and not retried and after is not None:
            await store.set(stored.seq, resume_from=after, pending_high=high, floor=settled_floor)
            log.warning("lineage_feed_walk_parked", extra={"resume_from": after, "pending_high": high, "scanned": scanned})
        raise LineageFeedBudgetExceeded(f"the lineage feed reconcile pass exceeded its {budget_seconds}s budget") from exc

    # `stored` is not None from here: the no-cursor branch returned inside the walk, and the checker
    # narrows it accordingly — a guard here is provably dead code rather than defence.
    if truncated:
        # Loud, and an ERROR rather than a warning: the walk covers the feed's whole retention by
        # default, so running out of pages means the rows between here and the cursor are already
        # pruned. They are unrecoverable, so stalling would buy nothing and only hide it.
        log.error("lineage_feed_gap_skipped", extra={"cursor": stored.seq, "resumed_at": high, "pages": max_pages})
    if gapped:
        # A DIFFERENT loss from the one above, and the one that used to be invisible. `truncated` means
        # the walk gave up; this means lineage's retention DELETED rows this lane had not read, so the
        # walk reached a clean end over a hole. ERROR for the same reason: unrecoverable, so there is
        # nothing to wait for and every tick that hides it makes the estate look healthier than it is.
        #
        # Not a stall. The rows are gone whatever this pass does, and holding the mark would forfeit
        # every row ABOVE them as well — the same trade the out-of-pages branch already makes.
        log.error("lineage_feed_pruned_below_cursor", extra={"cursor": stored.seq, "scanned": scanned})
        record_feed_gap()
    if retried:
        # A retry holds the mark — unless it has held it for FEED_MAX_STALLS consecutive passes, which
        # is a PERMANENT failure wearing a transient's costume. Step over it, loudly: the rows asking
        # for the retry are lost, and every row above them is delivered instead of joining them.
        stalls = stored.stalls + 1
        if stalls < FEED_MAX_STALLS:
            await store.set(stored.seq, floor=settled_floor, stalls=stalls)
            return ReconcileResult(scanned=scanned, retried=retried, cursor=stored.seq, truncated=truncated, gapped=gapped)
        log.error(
            "lineage_feed_poison_skipped",
            extra={"cursor": stored.seq, "resumed_at": high, "retried": retried, "stalls": stalls},
        )
        await store.set(high, floor=settled_floor)
        return ReconcileResult(scanned=scanned, retried=retried, cursor=high, truncated=truncated, gapped=gapped)
    if not retried:
        # No `resume_from`/`pending_high`: a completed walk settles the mark AND clears whatever an
        # earlier interrupted pass parked, so the next tick starts from the top again.
        # The floor rides along on every settle: a completed walk clears `resume_from`/
        # `pending_high`, but the backlog this deployment skipped is not something a later pass may
        # forget — dropping it here would re-open the overlap onto that backlog on the very next tick.
        await store.set(high, floor=settled_floor)
    # The cursor REPORTED is the one now in effect, not the highest row seen: a tick that hit a
    # transient failure leaves the mark where it was, and a result claiming otherwise would make the
    # log say the walk had made progress it deliberately did not make.
    return ReconcileResult(scanned=scanned, retried=retried, cursor=stored.seq if retried else high, truncated=truncated, gapped=gapped)


async def _prime(client: LineageFeedClient, store: LineageCursorStore) -> ReconcileResult:
    """First tick here: adopt the newest row as the mark and tell nobody about the history behind it."""
    page = await client.page(after=None)
    if not page.events:
        return ReconcileResult(primed=True)
    # `max` rather than `events[0]`: the server orders newest-first, and reading the mark off a
    # position would make this silently wrong the day that ordering is ever revisited.
    newest = max(record.seq for record in page.events)
    # The mark AND the floor: everything at or below `newest` is backlog this deployment chose not to
    # replay, so the overlap must never reach back into it. Recorded here, where the decision is made.
    await store.set(newest, floor=newest)
    log.info("lineage_feed_cursor_primed", extra={"cursor": newest})
    return ReconcileResult(cursor=newest, primed=True)
