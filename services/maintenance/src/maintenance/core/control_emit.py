"""Best-effort control-plane change-event emission from the MAINTENANCE service (#79).

The catalog publishes governance mutations (grants, warehouses, policies, namespaces, tables) onto the
broadcast ``catalog.control.v1`` topic. Since #79 this service makes one too: the expired-trash purge
destroys a dropped object's bytes and revokes its grants, which is exactly the class of change the
control stream exists to announce.

Shape mirrors ``catalog/core/control_emit.py`` deliberately — same Protocol, same no-op/Dapr pair, same
fail-open posture — but it is a SEPARATE module rather than an import: maintenance may not import the
catalog (``tests/unit/test_declared_dependencies.py``; declaring it would drag the whole catalog closure
into this image). The shared code is the event MODEL (``service_kit.control_events``), which is where the
wire contract belongs.

Fail-open, for a reason specific to this caller: the purge has already deleted bytes and revoked tuples by
the time it emits. A publish that raised there would fail a reclamation that already, irreversibly,
happened — so every error is swallowed, counted and logged, exactly as on the catalog side.

Two ways this fails SILENTLY in-cluster, both worth naming because "best-effort" hides them:

* the pubsub component's ``scopes`` must list this service's Dapr app-id, or the sidecar rejects every
  publish (same class as the secret-store scoping rule);
* with ``control_emit_enabled`` off, or no sidecar client, this is a no-op that publishes nowhere —
  never a half-configured transport pretending to emit.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from dapr.aio.clients import DaprClient
from opentelemetry import metrics

from service_kit import dapr_publish
from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent, ControlAction, ControlObjectType


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.maintenance")
_emit_failed = _meter.create_counter(
    "maintenance.control_emit.failed",
    unit="{event}",
    description="Best-effort maintenance control-plane emits that failed terminally (fail-open: the "
    "reclamation still happened, only the live-refresh hint is lost).",
)


@runtime_checkable
class ControlEmitter(Protocol):
    """Emit one control-plane change-event. Total (never raises)."""

    async def emit(self, event: CatalogControlEvent) -> None: ...


class NoopControlEmitter:
    """The off state (control emission disabled, or no Dapr transport). Every emit is a no-op."""

    async def emit(self, event: CatalogControlEvent) -> None:
        del event  # off state: no-op (`del` rather than a per-line ARG002 suppression, so BOTH ruff and
        # ty accept the unused arg; the param must stay named `event` to match the Protocol structurally)


class DaprControlEmitter:
    """Publish to the Dapr ``pubsub.jetstream`` component via the local sidecar.

    Bounded by a tight per-publish timeout (a hung sidecar must not stall a cron tick) and fully
    best-effort: every error is swallowed and counted.
    """

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
            _emit_failed.add(1, {"lance.maintenance.action": event.action})
            log.warning(
                "maintenance_control_publish_failed",
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
    """The chosen emitter: a Dapr publisher when enabled AND a sidecar client is present, else the no-op.

    Built once in the service lifespan onto ``app.state.control_emitter``, mirroring the lineage emitter.
    """
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
    """Build + emit a ``CatalogControlEvent`` (best-effort — the emitter swallows every error).

    Call AFTER the mutation succeeds, so a change that did not happen is never announced. Fail-open
    covers the WHOLE path including the pydantic construction: a malformed event degrades to no
    live-refresh hint rather than raising into a reclamation that already deleted bytes.
    """
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
            "maintenance_control_event_build_failed",
            extra={"action": action, "object_id": object_id, "error": str(exc)},
        )
        return
    await emitter.emit(event)
