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

**Tenacity lives here and nowhere else in this service.** This is the service's OWN egress: no sidecar
policy covers it, so the retry has to. The subscription handler has the opposite rule — the sidecar
owns redelivery there, and a second layer would multiply it.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt, stop_after_delay, wait_exponential_jitter

from notifications.api.fanout import InboxOpener
from notifications.api.ingest import DAPR_RETRY, ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.visibility import Visibility
from service_kit.exceptions import ServiceUnavailableError


log = logging.getLogger(__name__)

#: The state key the cursor lives under. One key for the service (not per subject), so it carries the
#: app's own name rather than going through the user-state key derivation.
CURSOR_KEY: Final = "notifications-lineage-cursor"

_RETRY_ATTEMPTS: Final = 4
_RETRY_BUDGET_SECONDS: Final = 30.0


class LineageCursorUnreadable(ServiceUnavailableError):
    """The stored cursor exists but cannot be read — never reported as absent.

    The distinction is the whole point. Absent means "prime and notify nobody", so an unreadable
    cursor read as absent would silently jump the high-water mark to the newest row and drop every
    notification in between — an outage turning into permanent, invisible data loss. Unreadable makes
    the tick fail instead, which an operator can see.
    """


class LineageCursor(BaseModel):
    """The reconciler's high-water mark: every feed row at or below `seq` has been handled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=0)
    updated_at: datetime


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


def _is_transient(exc: BaseException) -> bool:
    """Worth retrying: a transport fault, or a 429/5xx from lineage.

    Everything else is permanent and retrying it is pure delay — a 400 will be a 400 again, and a 401
    or 403 means this deployment's service identity is not configured, which no amount of backoff
    fixes. `ValueError` is deliberately absent for the same reason: a payload that will not parse is
    not a network problem.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == httpx.codes.TOO_MANY_REQUESTS or status >= httpx.codes.INTERNAL_SERVER_ERROR
    return False


def _log_retry(state: RetryCallState) -> None:
    """Log every retry — silent retries hide an outage behind a slow tick."""
    exc = state.outcome.exception() if state.outcome else None
    log.warning("lineage_feed_retry", extra={"attempt": state.attempt_number, "exception_type": type(exc).__name__ if exc else None})


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

    @retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(_RETRY_ATTEMPTS) | stop_after_delay(_RETRY_BUDGET_SECONDS),
        wait=wait_exponential_jitter(initial=0.5, max=5.0),
        before_sleep=_log_retry,
        reraise=True,
    )
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

    async def set(self, seq: int) -> None:
        """Move the high-water mark. Last write wins — one writer, no contender to race."""
        cursor = LineageCursor(seq=seq, updated_at=datetime.now(UTC))
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
    as well as the walk — the priming poll is a full tenacity-retried request, so a first-ever tick
    against a sick lineage is the longest thing this function can do and the one an unbounded version
    would let stack. Only the cursor WRITE is outside it, deliberately: a walk that finished inside the
    budget must not lose the progress it made to a timer firing during the write that records it.
    """
    scanned = 0
    retried = 0
    after: int | None = None
    truncated = True
    async with asyncio.timeout(budget_seconds):
        stored = await store.get()
        if stored is None:
            return await _prime(client, store)
        high = stored.seq
        for _ in range(max_pages):
            page = await client.page(after=after)
            fresh = [record for record in page.events if record.seq > stored.seq]
            for record in fresh:
                scanned += 1
                high = max(high, record.seq)
                status = await ingest_run_event(
                    record.event,
                    lane=Lane.FEED,
                    visibility=visibility,
                    open_inbox=open_inbox,
                    event_seq=record.seq,
                )
                if status == DAPR_RETRY:
                    retried += 1
            if len(fresh) < len(page.events) or page.next_cursor is None:
                # The cursor came into view, or the feed floor did — either way the walk is complete.
                truncated = False
                break
            after = page.next_cursor

    if truncated:
        # Loud, and an ERROR rather than a warning: the walk covers the feed's whole retention by
        # default, so running out of pages means the rows between here and the cursor are already
        # pruned. They are unrecoverable, so stalling would buy nothing and only hide it.
        log.error("lineage_feed_gap_skipped", extra={"cursor": stored.seq, "resumed_at": high, "pages": max_pages})
    if not retried:
        await store.set(high)
    # The cursor REPORTED is the one now in effect, not the highest row seen: a tick that hit a
    # transient failure leaves the mark where it was, and a result claiming otherwise would make the
    # log say the walk had made progress it deliberately did not make.
    return ReconcileResult(scanned=scanned, retried=retried, cursor=stored.seq if retried else high, truncated=truncated)


async def _prime(client: LineageFeedClient, store: LineageCursorStore) -> ReconcileResult:
    """First tick here: adopt the newest row as the mark and tell nobody about the history behind it."""
    page = await client.page(after=None)
    if not page.events:
        return ReconcileResult(primed=True)
    # `max` rather than `events[0]`: the server orders newest-first, and reading the mark off a
    # position would make this silently wrong the day that ordering is ever revisited.
    newest = max(record.seq for record in page.events)
    await store.set(newest)
    log.info("lineage_feed_cursor_primed", extra={"cursor": newest})
    return ReconcileResult(cursor=newest, primed=True)
