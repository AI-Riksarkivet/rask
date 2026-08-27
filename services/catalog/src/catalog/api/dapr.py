"""Dapr pub/sub subscription for the catalog's own control-plane change-event stream.

The catalog **publishes** ``CatalogControlEvent``s (``core/control_emit.py``) onto the dedicated broadcast
component ``catalog-control-pubsub`` (topic ``catalog.control.v1``) and also **subscribes** to it here —
**without** a ``queueGroupName``, so **every** catalog replica receives **every** event (broadcast fan-out,
not a competing-consumer group) and appends it to its own per-replica ring buffer
(``core/control_buffer.py``). The poll endpoint (``GET /v1/events``) then serves each replica's buffer to
the console. There is **no broker client in app code** — the Dapr sidecar owns delivery; this is a plain
HTTP route the sidecar POSTs each event to.

``DaprApp(app)`` always serves GET ``/dapr/subscribe`` (the registration the sidecar reads at startup); the
subscription itself is registered **only** when control eventing is enabled (``control_emit_enabled``,
matching the emitter gate in ``main.py``), so an HTTP-only / control-off deployment carries no always-live
ingest route. The thin ``main`` builds ``app`` first, then calls :func:`register_control_dapr` so the
subscription is wired after ``app`` exists (the same import-order constraint as lineage's split).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request
from pydantic import ValidationError

from catalog.api.dependencies import SettingsDep
from catalog.core.config import get_settings
from catalog.services import warehouses
from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)


async def on_control_event(
    body: dict[str, Any],
    request: Request,
    settings: SettingsDep,
    _: Annotated[None, Depends(require_dapr_token)],
) -> dict[str, str]:
    """Buffer one Dapr-delivered control-plane CloudEvent into this replica's ring buffer.

    ``body["data"]`` is the ``CatalogControlEvent`` (Dapr parses it since we publish with
    ``datacontenttype=application/json``); FastAPI has already parsed the JSON envelope into a dict, and a
    missing/malformed ``data`` field is caught by ``model_validate`` below. Authenticated by the Dapr
    **app-api-token** (``require_dapr_token``): without it
    the route is an unauthenticated, publicly-reachable ingest path — a forged POST could inject fabricated
    change-events into every replica's buffer (and thence the admin console). A malformed event is
    **dropped** (ack, not retried — events are hints and the audit trail is the durable record); a valid one
    is appended (``event_id`` dedupes a redelivery). We always ack ``SUCCESS``: the buffer is best-effort, so
    a drop only costs the client a redundant re-read on the next poll.

    This route is ALSO the cross-replica cache-invalidation path (#46): the warehouse-binding cache
    assumed bindings are immutable, and the warehouse delete broke that premise — the deleting
    replica popped its own cache while every other replica kept routing a dropped namespace at the
    deleted warehouse's bucket. The broadcast subscription is precisely "every replica hears every
    mutation", so the eviction lives here; the redundant self-delivery on the publishing replica is
    a harmless second pop."""
    data = body.get("data")
    try:
        event = CatalogControlEvent.model_validate(data)
    except (ValidationError, TypeError, ValueError) as exc:
        log.warning("control_event_invalid", extra={"error": str(exc)})
        return {"status": "SUCCESS"}
    buffer = getattr(request.app.state, "control_buffer", None)
    if buffer is not None:
        buffer.append(event)
    cache = getattr(request.app.state, "warehouse_binding_cache", None)
    if cache is not None:
        evicted = warehouses.evict_stale_bindings(cache, action=event.action, object_id=event.object_id, extra=event.extra, delimiter=settings.delimiter)
        if evicted:
            log.info("binding_cache_evicted", extra={"action": event.action, "object_id": event.object_id, "evicted": evicted})
    return {"status": "SUCCESS"}


def register_control_dapr(app: FastAPI) -> None:
    """Wire the broadcast control-event subscription onto ``app``.

    ``DaprApp(app)`` is always constructed (it adds GET ``/dapr/subscribe``). The subscription is registered
    **only** when control eventing is enabled (``control_emit_enabled``), matching the emitter gate in
    ``main.py``, so an HTTP-only / control-off deployment carries no always-live ingest route. There is **no**
    ``queueGroupName`` (broadcast: every replica buffers every event) and **no** dead-letter topic — the ring
    buffer tolerates loss by design (drop-oldest + ``reset`` on a cursor gap), so a parked delivery would add
    nothing over the client's next re-read."""
    # NOT injectable, and deliberately so: this runs at STARTUP wiring time, where there is no request
    # and therefore no dependency graph to resolve against. `app.dependency_overrides` cannot reach it
    # by construction, so a test that needs different settings here monkeypatches — which is the honest
    # answer at this boundary rather than the default habit it looks like elsewhere. The REQUEST-time
    # readers above take theirs by injection.
    settings = get_settings()
    dapr_app = DaprApp(app)
    if settings.control_emit_enabled:
        dapr_app.subscribe(pubsub=settings.control_pubsub, topic=CONTROL_TOPIC, route="/control-events")(on_control_event)
