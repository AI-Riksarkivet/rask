"""The forwarder: every recorded event, as standard OpenLineage, to a configured graph backend (ADR 0158).

The lineage graph is bring-your-own. A backend built for it (Marquez, DataHub, OpenMetadata) takes standard
OpenLineage over the HTTP transport, which POSTs each event as JSON to ``<url>/<endpoint>`` (``api/v1/lineage`` by
default), with ``Authorization: Bearer <api key>`` when the backend asks for one. With no URL configured, nothing is
forwarded.

AT LEAST ONCE, FROM THE FEED. The relay tick reads the feed forward from the forwarder's cursor, POSTs each event in
seq order, and moves the cursor past what the backend took. A crash between a POST and the cursor's write sends
those events again; nothing the feed holds is skipped. The cursor is a row of the catalog-kept ``cursors`` table,
keyed by the endpoint, so a new backend starts from the feed's oldest row and a backend's progress survives a
restart. It moves only forward, because the ticks of two replicas overlap and a slower one's write must not undo a
faster one's progress.

WHAT STOPS A TICK AND WHAT DOES NOT. A transport failure, a timeout, or any answer but a success or a refusal of the
event stops the tick with the cursor at the last event the backend took, and the next tick resumes there: the
backend is down, or it refuses the forwarder itself (a 401 or 403 is a key to fix, not an event to skip). A 400, 413
or 422 is the backend refusing that one event; retrying it would hold back every event after it, so it is counted
and passed.
"""

from __future__ import annotations

import logging
from typing import Final

import httpx
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from lineage.core import metrics
from lineage.core.config import LineageSettings
from lineage.services.provenance import ProvenanceStore
from service_kit.governed.secrets import fetch_required_secrets


log = logging.getLogger(__name__)

#: The answers that refuse one event rather than the forwarder: an event the backend judges malformed or too large.
_REFUSED: Final = frozenset({400, 413, 422})
#: Events read from the feed at a time. An event may weigh 1 MiB (`lineage.models.MAX_EVENT_BYTES`), so a page bounds
#: what one tick holds in memory whatever a producer sent.
_PAGE: Final = 32


class ForwardOutcome(BaseModel):
    """What one tick's forward did; ``stalled`` says why it stopped short of the feed's head, ``None`` when it did not."""

    forwarded: int = 0
    refused: int = 0
    stalled: str | None = None


def endpoint_url(settings: LineageSettings) -> str:
    """Where the events are POSTed: the HTTP transport's ``url`` joined to its ``endpoint``."""
    return f"{settings.forward_url.rstrip('/')}/{settings.forward_endpoint.lstrip('/')}"


async def _authorization(settings: LineageSettings) -> dict[str, str]:
    """The bearer the backend asks for, read from the secret store on every tick, so a rotated key needs no restart."""
    if not settings.forward_api_key_secret:
        return {}
    bundle = await run_in_threadpool(
        fetch_required_secrets,
        settings.dapr_secret_store,
        settings.forward_api_key_secret,
        require=settings.forward_api_key_field,
        timeout=2.0,
        retries=1,
        backoff=0.1,
    )
    return {"Authorization": f"Bearer {bundle[settings.forward_api_key_field]}"}


async def _send(provenance: ProvenanceStore, settings: LineageSettings, client: httpx.AsyncClient, url: str, mark: int) -> tuple[int, ForwardOutcome]:
    """POST up to ``forward_batch`` events after ``mark``, a page at a time.

    Returns the seq of the last event the backend answered, which the cursor may move to, and what was sent.
    """
    reached, forwarded, refused = mark, 0, 0
    headers: dict[str, str] | None = None
    while forwarded + refused < settings.forward_batch:
        records = await provenance.list_events(limit=min(_PAGE, settings.forward_batch - forwarded - refused), since=reached)
        if not records:
            break
        if headers is None:
            headers = await _authorization(settings)
        for record in records:
            try:
                answer = await client.post(url, json=record.event, headers=headers)
            except httpx.HTTPError as exc:
                return reached, ForwardOutcome(forwarded=forwarded, refused=refused, stalled=f"{type(exc).__name__}: {exc}")
            if answer.is_success:
                forwarded += 1
            elif answer.status_code in _REFUSED:
                refused += 1
                log.warning("lineage_forward_event_refused", extra={"seq": record.seq, "status": answer.status_code, "detail": answer.text[:300]})
            else:
                return reached, ForwardOutcome(forwarded=forwarded, refused=refused, stalled=f"HTTP {answer.status_code}")
            reached = record.seq
    return reached, ForwardOutcome(forwarded=forwarded, refused=refused)


async def forward(provenance: ProvenanceStore, settings: LineageSettings, client: httpx.AsyncClient) -> ForwardOutcome:
    """POST the feed's events after the forwarder's cursor to the backend, in seq order, and move the cursor past them."""
    url = endpoint_url(settings)
    follower = f"forward:{url}"
    mark = await provenance.cursor(follower)
    bounds = await provenance.feed_bounds()
    if bounds is not None and bounds[1] < mark:
        # A cursor this feed never issued: it was taken on another feed (the store moved, or was restored).
        log.warning("lineage_forward_cursor_ahead_of_feed", extra={"cursor": mark, "head": bounds[1]})
        mark = bounds[0] - 1
        await provenance.reset_cursor(follower, mark)
    elif bounds is not None and mark and bounds[0] > mark + 1:
        log.error("lineage_forward_pruned_before_forwarded", extra={"cursor": mark, "oldest_seq": bounds[0]})
    reached, outcome = await _send(provenance, settings, client, url, mark)
    if reached > mark:
        await provenance.advance_cursor(follower, reached)
    metrics.record_forwarded(forwarded=outcome.forwarded, refused=outcome.refused, stalled=outcome.stalled is not None)
    if outcome.stalled:
        log.warning("lineage_forward_stalled", extra={"cursor": reached, "reason": outcome.stalled})
    return outcome
