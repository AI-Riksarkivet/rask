"""The inbox door: read your feed, mark rows read, dismiss one.

Mounted under `RASK_API_PREFIX` by `make_service_app`, so the deployed paths are
`/api/notifications/inbox…` — exactly what the gateway's `/api/notifications` row forwards
unrewritten. Neither side spells that prefix: this router contributes only `/notifications` and the
gateway interpolates the same env var into both halves of its row, so the two track each other
wherever it is set. What must never appear is a LITERAL on either side — a router prefix of
`/api/notifications` here, or a hardcoded pair there, is correct exactly while `RASK_API_PREFIX`
happens to be `/api` and 404s the moment it is not. That trap has been paid for once already.

**Every route is SUBJECT-DERIVED.** No path param, no body field and no header names an inbox: the
actor id is `encode_subject(token.sub)`, minted from the verified token, so there is no parameter
anyone could forget to check. That is also why the two mutations run no authorization check — a
subject marking their own rows read is not a decision anyone else has a stake in, and adding an FGA
call there would be authorization theatre over an object the caller already is.

**The read DOES re-check.** Pointers are stored, grants are not: a subject whose grant was revoked
after delivery still has the row, and the panel resolving it must not disclose an object they can no
longer see. So the page is filtered through `can_get_metadata` on the way out — the same rule the
lineage read path enforces, asked once per page rather than once per row.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from notifications.api.cursor import decode_cursor, encode_cursor
from notifications.api.schemas import DismissResult, InboxFeed, InboxRow, MarkResult, UnreadBadge
from notifications.api.security import CurrentSubject, VisibilityDep
from notifications.dependencies import ActorPlaneDep, NotificationsSettingsDep
from notifications.models import INBOX_PAGE_LIMIT_MAX, InboxDismiss, InboxFilter, InboxMark, InboxPage, InboxQuery
from notifications.proxies import inbox_for


router = APIRouter(prefix="/notifications", tags=["inbox"])

# The query parameters' published help, lifted out of the signature so the signature stays readable.
_STATE_HELP = "`unread` is what the badge counts; `all` is what the panel shows. Neither includes dismissed rows."
_LIMIT_HELP = "Rows per page; the service's configured default when omitted."
_CURSOR_HELP = "An opaque cursor from a previous page's `next_cursor`. Hand it back; do not construct one."


@router.get("/inbox")
async def get_inbox(
    subject: CurrentSubject,
    settings: NotificationsSettingsDep,
    visibility: VisibilityDep,
    _actor_plane: ActorPlaneDep,
    state: Annotated[InboxFilter, Query(description=_STATE_HELP)] = InboxFilter.ALL,
    limit: Annotated[int | None, Query(ge=1, le=INBOX_PAGE_LIMIT_MAX, description=_LIMIT_HELP)] = None,
    cursor: Annotated[str | None, Query(min_length=1, description=_CURSOR_HELP)] = None,
) -> InboxFeed:
    """One page of this subject's inbox, newest first.

    `next_cursor` is built from the last RAW row of the page rather than the last VISIBLE one, and
    that is what keeps paging from stalling: on a page where the visibility filter dropped everything,
    a cursor over the visible rows would be `null` and the client would stop at a wall of hidden rows
    with more of its own below them. Handing back the window's floor costs nothing — the hidden row is
    never returned either way — and lets the reader page past it. It is the same reasoning the lineage
    feed's `next_cursor` carries for the same situation.

    An empty page for a subject with no grants is a filter over their OWN inbox, not a refused
    request: there is nothing here to refuse, because the object of every route in this module is the
    caller. The refusals this door does issue name themselves — 401 without a bearer when OIDC is on,
    503 when authorization is enabled but unwired, 503 when this process holds no actor plane.
    """
    query = InboxQuery(
        state=state,
        limit=limit if limit is not None else settings.inbox_page_limit,
        after=decode_cursor(cursor) if cursor else None,
    )
    page = InboxPage.model_validate(await inbox_for(subject).page(query.model_dump(mode="json")))
    allowed = await visibility.visible(subject, {pointer.object_id for pointer in page.pointers})
    return InboxFeed(
        # Projected onto the WIRE row, which drops the delivery ledger: what a reader may see is a
        # declared field list, never whatever the storage record happens to carry today.
        notifications=[InboxRow.of(pointer) for pointer in page.pointers if pointer.object_id in allowed],
        next_cursor=encode_cursor(page.pointers[-1]) if page.has_more and page.pointers else None,
        unread=page.unread,
    )


@router.get("/inbox/unread")
async def get_unread(subject: CurrentSubject, _actor_plane: ActorPlaneDep) -> UnreadBadge:
    """The badge count, answered from the actor's SMALL state partition alone.

    Its own route because the badge is polled and the feed is not: answering it through `GET /inbox`
    would load every pointer record to return one integer, which is precisely the cost the actor's
    two-partition split exists to avoid.

    Deliberately NOT visibility-filtered, and the reason is that it cannot be without paying the cost
    the route exists to avoid — the count is over the whole inbox, so filtering it would mean reading
    every row and batch-checking every object on every poll. The bound this accepts is narrow and
    stated: after a revocation the badge may count a row that no longer renders, until that row is
    dismissed or compacted away. Nothing is disclosed by the number — it names no object.
    """
    return UnreadBadge.model_validate(await inbox_for(subject).unread())


@router.post("/inbox/seen")
async def mark_seen(body: InboxMark, subject: CurrentSubject, _actor_plane: ActorPlaneDep) -> MarkResult:
    """Mark the rows the panel actually rendered as read — the shared bell's `onseen` seam.

    An empty id list is a no-op rather than a refusal: the panel legitimately renders nothing, and a
    call that errored on the empty case would make every caller branch on it.

    The actor answers with a row count alongside the badge; `MarkResult` does not declare it, so it
    does not reach the client. That is the return type doing its second job.
    """
    return MarkResult.model_validate(await inbox_for(subject).mark_seen(body.model_dump(mode="json")))


@router.post("/inbox/dismiss")
async def dismiss(body: InboxDismiss, subject: CurrentSubject, _actor_plane: ActorPlaneDep) -> DismissResult:
    """Dismiss one notification — the bell's `ondismiss` seam.

    Keyed by `run_id@STATE`, so dismissing a run's earlier state leaves its later one free to arrive:
    that is the whole reason the shared component's id scheme carries the state, and a backend keyed
    on the run alone would silence a FAILED run somebody had dismissed while it was still running.
    """
    return DismissResult.model_validate(await inbox_for(subject).dismiss(body.model_dump(mode="json")))
