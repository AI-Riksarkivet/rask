"""``GET /v1/events`` — the control-plane change-event poll endpoint (P2).

Serves this replica's in-memory ring buffer (``core/control_buffer.py``, fed by the broadcast Dapr
subscription in ``api/dapr.py``) to the admin console: events strictly after the caller's ``since`` cursor,
the new head cursor, and a ``reset`` flag when the cursor fell off the (bounded, drop-oldest) buffer. An
event is a **refresh hint**, never authoritative data — the console re-reads real state through the normal
FGA-governed path — so a lost/duplicated/late event only costs a redundant (or slightly delayed) re-read
(see ``control_buffer.py`` + ``docs/DECISIONS.md`` #control-events--fail-open-emit-contract).

**Estate-admin gated.** The feed is **estate-wide** — the buffer holds every replica's view of *every*
project's governance changes (broadcast subscription, no per-tenant partition). Observing it is therefore a
**platform** privilege, not a per-project one: the endpoint checks ``can_observe_events`` on the fixed root
object (``settings.fga_root_object`` = ``warehouse:lance_catalog``), an owner-tier action, so **authorization
scope == data scope** — a mere project admin gets a 403 (the #12 fix; the earlier per-project param let any
project admin read the whole estate). ``/v1/events`` matches no resource prefix in ``fga_deps.authorize`` so
the router-wide gate only sets the 401/503 floor; the ``can_observe_events`` decision is made here explicitly
(mirroring ``models.py``). A *meaningful* poll (events delivered or a reset) is audited
(``event_stream_opened``) — never an empty tick, so a 5s-polling console doesn't flood the audit trail.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.control_buffer import ControlEventBuffer
from catalog.schemas import EventsResponse
from service_kit.governed.audit import SUCCESS, audit


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/events", tags=["events"])


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
    when FGA is off (parity with every other ``require_*`` site). The ring buffer is per-replica + in-memory,
    so ``cursor`` is a per-replica monotonic counter and ``reset`` means the client missed the buffer window
    (scaling the catalog past one replica needs session affinity or a shared buffer — see docs/DECISIONS.md
    #control-events--per-replica-cursor-boundary)."""
    # Estate gate: can_observe_events on the fixed root object (NOT a caller-supplied project → no injectable
    # object id, and authz scope == the estate-wide data scope). 403 for a non-estate-admin, 503 on an FGA
    # outage, no-op when FGA is off.
    await fga_deps.require_relation(client, settings, token, relation="can_observe_events", obj=settings.fga_root_object)
    buffer: ControlEventBuffer | None = getattr(request.app.state, "control_buffer", None)
    events, cursor, reset = buffer.since(since) if buffer is not None else ([], 0, False)
    # Audit only a MEANINGFUL poll (events delivered or a reset), NEVER an empty 5s tick — else a polling
    # console floods the #41 audit trail. The gate's own allow/deny audit still records every authorized read.
    if events or reset:
        audit(
            "event_stream_opened",
            SUCCESS,
            subject=token.sub if token is not None else None,
            resource=settings.fga_root_object,
            delivered=len(events),
            reset=reset,
        )
    return EventsResponse(events=events, cursor=cursor, reset=reset)
