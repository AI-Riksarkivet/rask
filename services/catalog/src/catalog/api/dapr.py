"""Dapr pub/sub subscription for the catalog's own control-plane change-event stream.

The catalog **publishes** ``CatalogControlEvent``s (``core/control_emit.py``) onto the dedicated broadcast
component ``catalog-control-pubsub`` (topic ``catalog.control.v1``) and also **subscribes** to it here —
**without** a ``queueGroupName``, so **every** catalog replica receives **every** event (broadcast fan-out,
not a competing-consumer group) and appends it to its own per-replica ring buffer
(``core/control_buffer.py``). The poll endpoint (``GET /management/v1/events``) then serves each replica's buffer to
the console. There is **no broker client in app code** — the Dapr sidecar owns delivery; this is a plain
HTTP route the sidecar POSTs each event to.

``DaprApp(app)`` always serves GET ``/dapr/subscribe`` (the registration the sidecar reads at startup); the
subscription itself is registered **only** when control eventing is enabled (``control_emit_enabled``,
matching the emitter gate in ``main.py``), so an HTTP-only / control-off deployment carries no always-live
ingest route. The thin ``main`` builds ``app`` first, then calls :func:`register_control_dapr` so the
subscription is wired after ``app`` exists (the same import-order constraint as lineage's split).

**It acts only on what a control signer signed** ([[CTL-32]], ADR 0171). The ``dapr-api-token`` proves that this
app's sidecar delivered an event, and every app the bus lets publish reaches that sidecar, so the actor an event
names is a claim until its ``rask_signature`` proves it. The event is verified with the control doors' own rule,
against the identities of the role ``control_signer_role`` names for its action (``RASK_CONTROL_SIGNER_ROLES``),
under ``RASK_SIGNATURE_DOORS``: ``enforce`` acknowledges a refusal and buffers and evicts nothing for it, ``observe``
acts as ``off`` and counts what it would refuse, and keys the sidecar cannot serve are a retry, since they say nothing
about the signature. A refusal is counted in ``catalog.signature.refused``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final, get_args

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from opentelemetry import metrics
from pydantic import ValidationError

from catalog.api.control_relay import bounded, published_keys
from catalog.api.dependencies import SettingsDep
from catalog.core.config import Settings, get_settings
from catalog.services import warehouses
from lineage_kit.door import DoorMode, judge
from lineage_kit.signing import RefusalReason, control_signature_of, verify_control_signature
from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent, control_signer_role
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)

_SUCCESS: Final = {"status": "SUCCESS"}
_RETRY: Final = {"status": "RETRY"}

_meter = metrics.get_meter("lance.catalog")
_signature_refused = _meter.create_counter(
    "catalog.signature.refused",
    unit="{event}",
    description="Control events the catalog's enforcing subscription refused, by reason: acknowledged, buffered nowhere and evicting nothing.",
)
_signature_would_refuse = _meter.create_counter(
    "catalog.signature.would_refuse",
    unit="{event}",
    description="Control events the catalog's observing subscription acted on that enforce would refuse, by reason (keys_unavailable: the keys could not be read).",
)


def create_signature_refused_series() -> None:
    """Create every reason series of `catalog.signature.refused` at 0.

    The alert over this counter is `rate(...) > 0`, and a cumulative counter exports nothing for a series before its first
    add: a series born at 1 has no 0 before it, so `rate()` sees no increase and the first refusal never pages. The catalog
    runs under `opentelemetry-instrument`, whose MeterProvider is installed before this module is imported.
    """
    for reason in get_args(RefusalReason.__value__):
        _signature_refused.add(0, {"lance.catalog.reason": reason})


async def _screen(arrived: dict[str, Any], event: CatalogControlEvent, request: Request, settings: Settings) -> dict[str, str] | None:
    """None when the subscription may act on ``event``; otherwise what the sidecar is answered instead.

    The signature is checked over ``arrived``, the CloudEvent's data exactly as delivered: a model dump grows the fields
    a schema defaults, and the signer signed what it wrote.
    """
    mode = DoorMode(settings.signature_doors)
    if mode is DoorMode.OFF:
        return None
    signers = settings.control_signer_roles.get(control_signer_role(event.action), frozenset())
    claim = control_signature_of(arrived)
    source = published_keys(request, settings).for_event(claim.kid if claim else None)
    verdict = await run_in_threadpool(
        judge, lambda: verify_control_signature(arrived, source=source, signers=signers, delegators=settings.event_delegators), mode
    )
    logged = {"identity": bounded(claim.identity) if claim else None, "event_id": bounded(event.event_id), "action": event.action}
    if verdict.observed is not None:
        _signature_would_refuse.add(1, {"lance.catalog.reason": verdict.observed})
        log.warning("catalog_signature_would_refuse", extra={**logged, "reason": verdict.observed})
    if verdict.act:
        return None
    if verdict.refused is None:
        log.warning("catalog_signature_keys_unavailable", extra=logged)
        return _RETRY
    _signature_refused.add(1, {"lance.catalog.reason": verdict.refused})
    log.warning("catalog_signature_refused", extra={**logged, "reason": verdict.refused})
    return _SUCCESS


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
        return _SUCCESS
    # Screened whatever its shape: an envelope that is not a mapping carries no signature, so it is judged unsigned.
    if (answer := await _screen(data if isinstance(data, dict) else {}, event, request, settings)) is not None:
        return answer
    buffer = getattr(request.app.state, "control_buffer", None)
    if buffer is not None:
        buffer.append(event)
    cache = getattr(request.app.state, "warehouse_binding_cache", None)
    if cache is not None:
        evicted = warehouses.evict_stale_bindings(cache, action=event.action, object_id=event.object_id, extra=event.extra, delimiter=settings.delimiter)
        if evicted:
            log.info("binding_cache_evicted", extra={"action": event.action, "object_id": event.object_id, "evicted": evicted})
    return _SUCCESS


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
        create_signature_refused_series()
