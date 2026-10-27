"""The control lane's second path: a parked governance notification is kept and re-driven until it lands (LH-331).

A run notification that dead-letters still reaches its person, because the reconcile cron re-reads lineage's durable
feed. A governance notification (a grant, a revoke, an erasure release) has no such feed: the catalog's
`/management/v1/events` is a per-replica ring of hints, so once `dlq.notifications` takes one, nothing re-presents it.
So the dead-letter route keeps each control event it is handed here, and every reconcile tick offers each kept event to
the same signature door and the same idempotent control ingest the bus route uses. Delivery dedupes on
`<event_id>@<ACTION>`, so a re-drive cannot tell anyone twice.

**One key per event, and an index of their ids**, in the sidecar's plain state API beside the lineage cursor (the parks
belong to the service, not to a subject). The state API cannot list keys, so `control-parks` lists the parked ids, and
each event lives under its own `control-park-<event_id>`: a park or a retire rewrites one event and a list of ids, never
the other events. The event is written before its id is listed, so the index never names an event that was not kept; a
listed id whose event is gone is retired on the next tick. Every write is `first-write`: the index is written against
the ETag it read, and an event key is insert-only, so two writers cannot overwrite each other. Measured on the live
sidecar (daprd with `state.postgresql`, 2026-10-27): a stale ETag answers 409, and an insert-only write of a key that
exists answers 500 `ERR_STATE_SAVE` "no item was updated". A store this module cannot read or write fails closed.

**Kept for as long as the dead-letter stream would have kept it** (`dlq.>`, max-age 168h in
`chart/templates/nats-stream-job.yaml`): after that the event is retired as expired, counted, and alerted on, since the
person was never told. Nothing else drops a park.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from notifications.api.control_events import ParkForgetter, named_person
from notifications.api.metrics import ParkOutcome, record_control_park
from service_kit.control_events import CatalogControlEvent
from service_kit.exceptions import ServiceUnavailableError


log = logging.getLogger(__name__)

#: The state key listing every parked event id.
INDEX_KEY = "control-parks"
#: How long a park is re-driven: the dead-letter stream's own retention.
PARK_HORIZON = timedelta(hours=168)
#: How often an index write that lost a race to another writer is read and applied again before the call gives up.
_CONFLICT_ATTEMPTS = 3


def park_key(event_id: str) -> str:
    """The state key one parked event lives under. The id is encoded: it travels in the sidecar's URL path."""
    return f"control-park-{quote(event_id, safe='')}"


class ParkedControl(BaseModel):
    """One kept control event: the CloudEvent's data as it dead-lettered, and when it was parked."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: str
    event: dict[str, Any]
    parked_at: datetime


class ParkIndex(BaseModel):
    """The ids of every kept event. Service-internal and written by this module alone, so an extra field is ignored."""

    model_config = ConfigDict(extra="ignore")

    event_ids: list[str] = Field(default_factory=list)


class ControlParksUnavailable(ServiceUnavailableError):
    """The park could not be read or written; the dead letter is retried rather than acked and lost."""


class RedriveResult(BaseModel):
    """What one re-drive pass did with the parks it reached."""

    delivered: int = 0
    kept: int = 0
    refused: int = 0
    expired: int = 0
    #: Parks the pass's time budget did not reach; offered on the next tick.
    deferred: int = 0


#: How a kept event is offered again: the bus route's signature door and control ingest, answering what became of it
#: (`DELIVERED`, `KEPT` for one to try on the next tick, or `REFUSED` for good).
type ControlIngest = Callable[[dict[str, Any]], Awaitable[ParkOutcome]]


class ControlParkStore:
    """The parks in the sidecar's plain state API: an insert-only key per event and an ETag-guarded index of their ids."""

    def __init__(self, *, client: httpx.AsyncClient, store_name: str, dapr_http_port: int = 3500, timeout_seconds: float = 5.0) -> None:
        self._client = client
        self._base = f"http://localhost:{dapr_http_port}/v1.0/state/{store_name}"
        self._timeout = timeout_seconds

    async def _get(self, key: str) -> httpx.Response | None:
        """The stored value's response, or None when Dapr answers 204 for a key that holds nothing."""
        try:
            response = await self._client.get(f"{self._base}/{key}", timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ControlParksUnavailable("the control-notification park could not be read") from exc
        return None if response.status_code == httpx.codes.NO_CONTENT or not response.content else response

    async def _save(self, entry: dict[str, Any]) -> bool:
        """Write ``entry`` first-write; whether it was written, False when another writer got there first."""
        try:
            response = await self._client.post(self._base, json=[entry | {"options": {"concurrency": "first-write"}}], timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ControlParksUnavailable("the control-notification park could not be written") from exc
        if response.is_success:
            return True
        # 409 is a stale ETag; a 5xx on an insert-only write (no ETag) is daprd's "no item was updated" for a key that
        # exists. A store that is really down fails the read the caller makes next.
        if response.status_code == httpx.codes.CONFLICT or ("etag" not in entry and response.is_server_error):
            return False
        raise ControlParksUnavailable(f"the control-notification park could not be written ({response.status_code})")

    async def read_index(self) -> tuple[list[str], str | None]:
        """The parked ids and the index's ETag; empty with no ETag when nothing was ever parked."""
        response = await self._get(INDEX_KEY)
        if response is None:
            return [], None
        try:
            return ParkIndex.model_validate_json(response.content).event_ids, response.headers.get("etag")
        except ValidationError as exc:
            raise ControlParksUnavailable("the control-notification park index no longer fits its schema") from exc

    async def update_index(self, change: Callable[[list[str]], list[str]]) -> None:
        """Apply ``change`` to the parked ids and write them, again from a fresh read if another writer won."""
        for _ in range(_CONFLICT_ATTEMPTS):
            ids, etag = await self.read_index()
            entry: dict[str, Any] = {"key": INDEX_KEY, "value": ParkIndex(event_ids=change(ids)).model_dump(mode="json")}
            if etag is not None:
                entry["etag"] = etag
            if await self._save(entry):
                return
        raise ControlParksUnavailable("the control-notification park index kept changing under this write")

    async def insert_event(self, kept: ParkedControl) -> bool:
        """Keep ``kept`` under its own key; False when that event was already kept."""
        return await self._save({"key": park_key(kept.event_id), "value": kept.model_dump(mode="json")})

    async def read_event(self, event_id: str) -> ParkedControl | None:
        """The kept event, or None when its key holds nothing."""
        response = await self._get(park_key(event_id))
        if response is None:
            return None
        try:
            return ParkedControl.model_validate_json(response.content)
        except ValidationError as exc:
            raise ControlParksUnavailable("a parked control event no longer fits its schema") from exc

    async def delete_event(self, event_id: str) -> None:
        """Forget one kept event. Deleting a key that holds nothing is not an error."""
        try:
            response = await self._client.delete(f"{self._base}/{park_key(event_id)}", timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ControlParksUnavailable("a parked control event could not be deleted") from exc


def get_control_parks(request: Request) -> ControlParkStore:
    """The lifespan-built park store, or a 503: a dead letter with nowhere to be kept must not be acked."""
    store = getattr(request.app.state, "control_parks", None)
    if store is None:
        raise ControlParksUnavailable("the control-notification park is unavailable")
    return store


def parks_forgetter(request: Request) -> ParkForgetter | None:
    """How an erasure forgets the parks naming its person, or None when this process built no park store."""
    store = getattr(request.app.state, "control_parks", None)
    if store is None:
        return None

    async def forget(subject: str) -> None:
        await forget_parks_naming(store, subject)

    return forget


def control_event_id(data: object) -> str | None:
    """The event id of ``data`` when it is a control event, else None (a run event, or a payload that will not parse)."""
    try:
        return CatalogControlEvent.model_validate(data).event_id
    except ValidationError:
        return None


async def park(store: ControlParkStore, *, event_id: str, data: dict[str, Any]) -> bool:
    """Keep one dead-lettered control event and list its id; whether it was newly kept (False: kept already).

    The id is listed even when the event was kept already, so a dead letter redelivered after a failure between the two
    writes finishes the park it began.
    """
    fresh = await store.insert_event(ParkedControl(event_id=event_id, event=data, parked_at=datetime.now(UTC)))
    await store.update_index(lambda ids: ids if event_id in ids else [*ids, event_id])
    if fresh:
        record_control_park(ParkOutcome.PARKED)
    return fresh


async def forget_parks_naming(store: ControlParkStore, subject: str) -> None:
    """Forget every parked control event naming ``subject``: the person was offboarded and their inbox erased.

    A group grant a park holds is not expanded here, and it reaches the erased person only if they are still in the
    group, which their offboarding revoked.
    """
    ids, _ = await store.read_index()
    forgotten: set[str] = set()
    for event_id in ids:
        kept = await store.read_event(event_id)
        if kept is not None and control_event_id(kept.event) is not None and named_person(CatalogControlEvent.model_validate(kept.event)) == subject:
            await store.delete_event(event_id)
            forgotten.add(event_id)
    if forgotten:
        await store.update_index(lambda current: [event_id for event_id in current if event_id not in forgotten])
        log.info("control_parks_forgotten", extra={"forgotten": len(forgotten)})


async def redrive(store: ControlParkStore, *, ingest: ControlIngest, budget_seconds: float, now: datetime | None = None) -> RedriveResult:
    """Offer each kept event to ``ingest`` once, within ``budget_seconds``; retire what landed, what was refused for good
    and what expired, and leave what the budget did not reach for the next tick.

    An event parked while this pass ran is not touched by it: the retire removes only the ids this pass decided.
    """
    ids, _ = await store.read_index()
    at = now or datetime.now(UTC)
    outcomes: dict[str, ParkOutcome | None] = {}
    try:
        async with asyncio.timeout(budget_seconds):
            for event_id in ids:
                kept = await store.read_event(event_id)
                if kept is None:
                    outcomes[event_id] = None
                elif at - kept.parked_at > PARK_HORIZON:
                    outcomes[event_id] = ParkOutcome.EXPIRED
                    log.error("control_park_expired", extra={"event_id": event_id, "parked_at": kept.parked_at.isoformat()})
                else:
                    outcomes[event_id] = await ingest(kept.event)
    except TimeoutError:
        log.warning("control_park_redrive_budget_spent", extra={"reached": len(outcomes), "parked": len(ids)})
    for outcome in outcomes.values():
        if outcome is not None:
            record_control_park(outcome)
    retired = {event_id for event_id, outcome in outcomes.items() if outcome is not ParkOutcome.KEPT}
    for event_id in retired:
        await store.delete_event(event_id)
    if retired:
        await store.update_index(lambda current: [event_id for event_id in current if event_id not in retired])
    tally = list(outcomes.values())
    return RedriveResult(
        delivered=tally.count(ParkOutcome.DELIVERED),
        kept=tally.count(ParkOutcome.KEPT),
        refused=tally.count(ParkOutcome.REFUSED),
        expired=tally.count(ParkOutcome.EXPIRED),
        deferred=len(ids) - len(outcomes),
    )
