"""Best-effort control-plane change-event emission from the catalog onto the Dapr/NATS bus.

Mirrors `core/lineage_emit.py`'s Dapr transport, for the governance/metadata stream (grants, warehouses,
policies, namespaces, tables) instead of the OpenLineage data stream. Publish is **inline-awaited +
best-effort**: the mutation endpoints `await` it AFTER the backend/FGA mutation succeeds (so a real change
is never announced), but it swallows every error, so the bus being down/slow can never fail a catalog
mutation — the audit trail still records it, we just skip the live-refresh hint (fail-open, the
`lineage_emit` principle). No broker client in app code: we publish to the local Dapr sidecar via
`common.dapr_publish.publish_event` (the sidecar owns retry/backoff/DLQ as component config); the catalog
subscribes to the same topic WITHOUT a queueGroupName, so every replica gets every event (broadcast).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from common import dapr_publish
from common.control_events import (
    CONTROL_TOPIC,
    CatalogControlEvent,
    ControlAction,
    ControlObjectType,
)
from dapr.aio.clients import DaprClient
from opentelemetry import metrics


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_emit_failed = _meter.create_counter(
    "catalog.control_emit.failed",
    unit="{event}",
    description="Best-effort catalog control-plane emits that failed terminally (fail-open: the audit "
    "trail still records the change, only the live-refresh hint is lost).",
)


@runtime_checkable
class ControlEmitter(Protocol):
    """Emit one control-plane change-event. Total (never raises) — a bus failure degrades to no live
    refresh, never a failed mutation."""

    async def emit(self, event: CatalogControlEvent) -> None: ...


class NoopControlEmitter:
    """The off state (control emission disabled, or no Dapr transport). Every emit is a no-op."""

    async def emit(self, event: CatalogControlEvent) -> None:
        del event  # off state: no-op (`del`, not a per-line ARG002 suppression, so BOTH ruff and ty accept the unused arg;
        # the param must stay named `event` to structurally match the ControlEmitter protocol)


class DaprControlEmitter:
    """Publish the event to the Dapr `pubsub.jetstream` component via the local sidecar. Bounded by a tight
    per-publish timeout (a hung sidecar must not pin the inline-awaited emit on a mutation request path) and
    fully best-effort (every error swallowed + counted). `authorization` is irrelevant — the topic is an
    internal catalog-only channel, so the subscriber trusts the verified `actor` the catalog stamped."""

    def __init__(self, client: DaprClient, *, pubsub: str, topic: str, timeout_seconds: float) -> None:
        self._client = client
        self._pubsub = pubsub
        self._topic = topic
        self._timeout_seconds = timeout_seconds

    async def emit(self, event: CatalogControlEvent) -> None:
        try:
            await dapr_publish.publish_event(
                self._client,
                timeout_seconds=self._timeout_seconds,
                pubsub_name=self._pubsub,
                topic_name=self._topic,
                data=event.model_dump_json(),
                data_content_type="application/json",
            )
        except Exception as exc:
            _emit_failed.add(1, {"lance.catalog.action": event.action})
            log.warning(
                "control_publish_failed",
                extra={"action": event.action, "object_id": event.object_id, "error": str(exc)},
            )


def make_control_emitter(
    *,
    enabled: bool,
    dapr: DaprClient | None,
    pubsub: str,
    topic: str = CONTROL_TOPIC,
    timeout_seconds: float,
) -> ControlEmitter:
    """The chosen control emitter: a Dapr publisher when enabled + a sidecar client is present, else the
    no-op (dev/off, like lineage). Built once in the catalog lifespan onto `app.state.control_emitter`."""
    if enabled and dapr is not None:
        return DaprControlEmitter(dapr, pubsub=pubsub, topic=topic, timeout_seconds=timeout_seconds)
    return NoopControlEmitter()


async def emit_control(
    emitter: ControlEmitter,
    *,
    action: ControlAction,
    object_type: ControlObjectType,
    object_id: str,
    actor: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Build + emit a `CatalogControlEvent` on `emitter` (best-effort — the emitter swallows every error).

    Call at a mutation endpoint AFTER the backend/FGA change + its audit succeed, so a real change is never
    announced. Endpoints obtain `emitter` via `ControlEmitterDep` (mirrors `LineageEmitterDep`); the off
    state is a `NoopControlEmitter` (the dependency's fallback), so the call is always safe — no `getattr`
    guard needed at the call site. `actor` must be the verified OIDC subject (e.g. `user:alice`), never a
    request-body value.

    Fail-open covers the WHOLE path, not just the publish: the `CatalogControlEvent` construction (pydantic
    validation) is wrapped too, so a malformed event can never raise into — and 500 — a mutation that already
    committed. A build failure degrades to no live-refresh hint, exactly like a publish failure."""
    try:
        event = CatalogControlEvent(
            action=action,
            object_type=object_type,
            object_id=object_id,
            actor=actor,
            extra=extra or {},
        )
    except Exception as exc:
        log.warning(
            "control_event_build_failed",
            extra={"action": action, "object_id": object_id, "error": str(exc)},
        )
        return
    await emitter.emit(event)
