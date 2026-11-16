"""``GET /management/v1/events`` — the control-plane change-event poll endpoint (P2).

Serves the control topic's durable stream to the admin console (ADR 0216): the events after the
caller's ``since`` cursor, read by sequence (`lakehouse.bus.Bus.read`), the new cursor, and a ``reset`` flag when the
stream no longer holds what came after the cursor. The cursor is the stream's sequence, the same on every replica, so any
replica answers any poller and the Service needs no affinity. An event is served only when the control doors' rule admits
it (``api/control_lane.judge``); one whose keys cannot be read yet ends the page before it, so it is served once they are.
An event is a **refresh hint**, never authoritative data — the console re-reads real state through the normal
FGA-governed path — so a late event only costs a slightly delayed re-read
(``docs/adr/0031-control-events-fail-open-emit-contract.md``).

**Estate-admin gated.** The feed is **estate-wide** — the stream holds *every*
project's governance changes (one topic, no per-tenant partition). Observing it is therefore a
**platform** privilege, not a per-project one: the endpoint checks ``can_observe_events`` on the fixed root
object (``settings.fga_root_object`` = ``estate:rask``), an owner-tier action, so **authorization
scope == data scope** — a mere project admin gets a 403 (the #12 fix; the earlier per-project param let any
project admin read the whole estate). The root being its OWN TYPE is what makes that structural rather than
a convention: no tenant can own an ``estate:`` object, so the privilege cannot be reached sideways. ``/management/v1/events`` matches no resource prefix in ``fga_deps.authorize`` so
the router-wide gate only sets the 401/503 floor; the ``can_observe_events`` decision is made here explicitly.
A *meaningful* poll (events delivered or a reset) is audited
(``event_stream_opened``) — never an empty tick, so a 5s-polling console doesn't flood the audit trail.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request
from lance_namespace import ServiceUnavailableError

from catalog.api import fga_deps
from catalog.api.control_lane import STREAM, TOPIC, control_event_in, judge
from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.schemas import EventsResponse
from lakehouse.bus import Bus, BusUnavailableError
from lakehouse.control_events import CatalogControlEvent
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit


log = logging.getLogger(__name__)

router = APIRouter(prefix="/management/v1/events", tags=["events"])


@router.get("")
async def poll_control_events(
    request: Request,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    since: Annotated[int, Query(ge=0, description="the last cursor the client saw (0 = baseline)")] = 0,
) -> EventsResponse:
    """Return control-plane change-events after ``since`` for a platform (estate) administrator.

    Fail-closed via FGA: a non-estate-admin gets 403, an OpenFGA outage 503, and the gate is a no-op only
    when FGA is off (parity with every other ``require_*`` site). A bus this process cannot read answers 503."""
    # Estate gate: can_observe_events on the fixed root object (NOT a caller-supplied project → no injectable
    # object id, and authz scope == the estate-wide data scope). 403 for a non-estate-admin, 503 on an FGA
    # outage, no-op when FGA is off.
    await fga_deps.require_relation(client, settings, token, relation="can_observe_events", obj=settings.fga_root_object)
    events, cursor, reset = await _served(request, settings, since)
    # Audit only a MEANINGFUL poll (events delivered or a reset), NEVER an empty 5s tick — else a polling
    # console floods the #41 audit trail. The gate's own allow/deny audit still records every authorized read.
    if events or reset:
        audit(
            AuditAction.EVENT_STREAM_OPENED,
            AuditOutcome.SUCCESS,
            subject=token.subject if token is not None else None,
            resource=settings.fga_root_object,
            delivered=len(events),
            reset=reset,
        )
    return EventsResponse(events=events, cursor=cursor, reset=reset)


async def _served(request: Request, settings: Settings, since: int) -> tuple[list[CatalogControlEvent], int, bool]:
    """The events the stream holds after ``since`` that the doors admit, the cursor after them, and whether the stream
    no longer holds what came after ``since``. A process that emits no control events, or is named no bus, serves none."""
    bus: Bus | None = getattr(request.app.state, "bus", None)
    if bus is None or not settings.control_emit_enabled:
        return [], 0, False
    try:
        page = await bus.read(STREAM, TOPIC, after=since, limit=settings.control_page_size)
    except BusUnavailableError as exc:
        raise ServiceUnavailableError("the control events could not be read") from exc
    events: list[CatalogControlEvent] = []
    cursor = min(since, page.end)
    for message in page.messages:
        carried = control_event_in(message)
        if carried is not None:
            arrived, event = carried
            verdict = await judge(request.app, settings, arrived, event)
            if verdict.retry:
                break
            if verdict.act:
                events.append(event)
        cursor = message.sequence
    return events, cursor, page.gap
